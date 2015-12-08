#
# =================================================================
# =================================================================
"""Table Creation/Deletion for Schema changes in the PowerVC 1.2.1 Release"""

from datetime import datetime
from sqlalchemy import MetaData, Boolean, Column, DateTime
from sqlalchemy import Integer, String, Text, Table
from sqlalchemy.sql import expression
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)

POWER_SPEC_COLUMNS = [
    Column('dedicated_sharing_mode', String(31)),
    Column('dedicated_proc', String(31)), Column('avail_priority', Integer),
    Column('shared_weight', Integer), Column('rmc_state', String(31)),
    Column('uncapped', Boolean), Column('operating_system', String(255)),
    Column('cpu_topology', Text)
]


def upgrade(migration_engine):
    """ Upgrades the Nova DB Tables to include those for PowerVC 1.2.1 """
    metadata = MetaData(migration_engine)
    metadata.reflect(migration_engine)
    metadata.bind = migration_engine

    try:
        #We need to add the additional columns to the PowerSpecs table
        spec_table = Table('instance_power_specs', metadata, autoload=True)
        for column in POWER_SPEC_COLUMNS:
            #Check first to see if the Column exists on the Table already
            if spec_table._columns.get(column.name) is None:
                spec_table.create_column(column)
        #Need to temporarily create server_vio2 table until Network DOM switch
        try:
            vios2_table = Table(
                'server_vio2', metadata,
                Column('created_at', DateTime), Column('updated_at', DateTime),
                Column('deleted_at', DateTime), Column('deleted', Integer),
                Column('pk_id', Integer, primary_key=True, nullable=False),
                Column('lpar_id', Integer, nullable=False),
                Column('lpar_name', String(length=255), nullable=False),
                Column('state', String(length=31)),
                Column('rmc_state', String(length=31)),
                Column('cluster_provider_name', String(length=255)),
                Column('compute_node_id', Integer, nullable=False))
            vios2_table.create(checkfirst=True)
        #Ignore the exception on the table create, since it is temporary
        except:
            pass
        #Next we need to try to add the default entries to the PowerSpecs table
        inst_table = Table('instances', metadata, autoload=True)
        inst_query = expression.select(
            [inst_table.c.uuid]).where(inst_table.c.deleted == 0)
        inst_uuids = migration_engine.execute(inst_query).fetchall()
        #Loop through each Instance UUID's, seeing if a PowerSpecs exists
        for uuid in inst_uuids:
            uuid = uuid[0]
            spec_query = expression.select(
                [spec_table.c.id]).where(spec_table.c.instance_uuid == uuid)
            #If the PowerSpec entry doesn't exist already, need to create it
            if migration_engine.execute(spec_query).fetchone() is None:
                spec_table.insert().values(
                    created_at=datetime.utcnow(), instance_uuid=uuid, deleted=0
                ).execute()
    except Exception as exc:
        LOG.exception(_('Exception updating instance_power_specs: %s') % exc)
        raise exc


def downgrade(migration_engine):
    """ Downgrades the Nova DB Tables to remove those for PowerVC 1.2.1 """
    pass
