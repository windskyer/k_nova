#
# =================================================================
# =================================================================
"""Table Creation/Deletion for Schema changes in the PowerVC 1.2 Release"""

from sqlalchemy import MetaData
from nova.openstack.common import log as logging
from paxes_nova.db.sqlalchemy import models
from paxes_nova.db.network import models as network_models

LOG = logging.getLogger(__name__)


def upgrade(migration_engine):
    """ Upgrades the Nova DB Tables to include those for PowerVC 1.2 """
    metadata = MetaData(migration_engine)
    metadata.reflect(migration_engine)
    metadata.bind = migration_engine

    #Loop through all of the DTO's that we added new in Paxes 1.2
    #creating the tables off of the definition defined in the DTO
    for dto in models.POWERVC_V1R2_DTOS:
        try:
            table = dto.__table__.tometadata(metadata, None)
            table.create(checkfirst=True)
        except Exception as exc:
            LOG.info(repr(dto.__table__))
            tbl = dto.__table__.__class__.__name__
            LOG.exception(_('Exception creating table %(tbl)s: %(exc)s') %
                          {'tbl': tbl, 'exc': exc})
            raise exc
    # Repeat DTO-like loop for (new) DOM-level tables
    for tblmap_class in network_models.POWERVC_V1R2_TABLEMAPPED_CLASSES:
        try:
            table = tblmap_class.__table__.tometadata(metadata, None)
            table.create(checkfirst=True)
            #print "Created table:", str(table)
        except Exception as exc:
            LOG.info(repr(tblmap_class.__table__))
            tbl = tblmap_class.__table__.__class__.__name__
            LOG.exception(_('Exception creating table %(tbl)s: %(exc)s') %
                          {'tbl': tbl, 'exc': exc})
            raise exc
    # Delete Instance metadata for all PVC keys
    pvc_keys = ("('min_vcpus', 'max_vcpus', 'pending_vcpus', "
                "'pending_min_vcpus', 'pending_max_vcpus', 'run_vcpus', "
                "'min_cpus', 'max_cpus', 'pending_cpus', "
                "'pending_min_cpus', 'pending_max_cpus', 'run_cpus', "
                "'min_memory_mb', 'max_memory_mb', 'pending_memory_mb', "
                "'pending_min_memory_mb', 'pending_max_memory_mb', "
                "'run_memory_mb', 'rmc_state', "
                "'current_compatibility_mode', 'desired_compatibility_mode', "
                "'vcpu_mode', 'vcpus', 'memory_mb', 'memory_mode', "
                "'cpu_utilization', 'pending_action')"
                )
    #conn = migration_engine.connect()
    #Cleanup the Instance System Meta-data Keys that have moved to PowerSpecs
    #Commenting out now until we actually move all data from System MetaData
    #(should also add result = conn.execute(...) and then result.close())
    #conn.execute('delete from INSTANCE_SYSTEM_METADATA where "key" in '
    #             + pvc_keys)
    #conn.close()


def downgrade(migration_engine):
    """ Downgrades the Nova DB Tables to remove those for PowerVC 1.2 """
    metadata = MetaData(migration_engine)
    metadata.reflect(migration_engine)
    metadata.bind = migration_engine

    #Loop through all of the DTO's that we added new in Paxes 1.2
    #dropping the tables off of the definition defined in the DTO
    for dto in reversed(models.POWERVC_V1R2_DTOS):
        try:
            dto.__table__.metadata = metadata
            dto.__table__.drop(checkfirst=True)
        except Exception as exc:
            LOG.info(repr(dto.__table__))
            tbl = dto.__table__.__class__.__name__
            LOG.exception(_('Exception dropping table %(tbl)s: %(exc)s') %
                          {'tbl': tbl, 'exc': exc})
            raise exc
    #
    for tblmap_class in reversed(
            network_models.POWERVC_V1R2_TABLEMAPPED_CLASSES):
        try:
            tblmap_class.__table__.metadata = metadata
            tblmap_class.__table__.drop(checkfirst=True)
        except Exception as exc:
            LOG.info(repr(tblmap_class.__table__))
            tbl = tblmap_class.__table__.__class__.__name__
            LOG.exception(_('Exception dropping table %(tbl)s: %(exc)s') %
                          {'tbl': tbl, 'exc': exc})
            raise exc
