import logging
from banal import first
from followthemoney import model

from aleph.core import db, archive
from aleph.model import Mapping
from aleph.queues import queue_task, OP_INDEX
from aleph.index.entities import get_entity
from aleph.index.collections import delete_entities
from aleph.logic.collections import update_collection
from aleph.logic.aggregator import get_aggregator, drop_aggregator

log = logging.getLogger(__name__)


def make_mapper(collection, mapping):
    table = get_entity(mapping.table_id)
    properties = table.get('properties', {})
    csv_hash = first(properties.get('csvHash'))
    if csv_hash is None:
        raise RuntimeError("Source table doesn't have a CSV version")
    url = archive.generate_url(csv_hash)
    if not url:
        local_path = archive.load_file(csv_hash)
        if local_path is not None:
            url = local_path.as_posix()
    if url is None:
        raise RuntimeError("Could not generate CSV URL for the table")
    data = {'csv_url': url, 'entities': mapping.query}
    return model.make_mapping(data, key_prefix=collection.foreign_id)


def load_mapping(stage, collection, mapping_id):
    """Flush and reload all entities generated by a mapping."""
    mapping = Mapping.by_id(mapping_id)
    if mapping is None:
        return log.error("Could not find mapping: %s", mapping_id)
    flush_mapping(stage, collection, mapping_id)
    mapper = make_mapper(collection, mapping)
    aggregator = get_aggregator(collection)
    try:
        writer = aggregator.bulk()
        entities_count = 0
        entity_ids = set()
        for idx, record in enumerate(mapper.source.records, 1):
            for entity in mapper.map(record).values():
                if entity.schema.is_a('Thing'):
                    entity.add('proof', mapping.table_id)
                entity = collection.ns.apply(entity)
                entity_ids.add(entity.id)
                entities_count += 1
                fragment = '%s-%s' % (mapping.id, idx)
                writer.put(entity, fragment=fragment)

            if idx > 0 and idx % 1000 == 0:
                stage.report_finished(1000)
                log.info("[%s] Loaded %s records, %s entities...",
                         collection.foreign_id,
                         idx, entities_count)

        writer.flush()
        log.info("[%s] Mapping done (%s entities)",
                 mapping.id, entities_count)

        payload = {'entity_ids': entity_ids, 'mapping_id': mapping.id}
        queue_task(collection, OP_INDEX, job_id=stage.job.id, payload=payload)
        mapping.set_status(status=Mapping.SUCCESS)
    except Exception as exc:
        mapping.set_status(status=Mapping.FAILED, error=str(exc))
    finally:
        aggregator.close()


def flush_mapping(stage, collection, mapping_id, sync=False):
    """Delete entities loaded by a mapping"""
    log.debug("Flushing entities for mapping: %s", mapping_id)
    delete_entities(collection.id, mapping_id=mapping_id, sync=True)
    drop_aggregator(collection)
    collection.touch()
    db.session.commit()
    update_collection(collection)
