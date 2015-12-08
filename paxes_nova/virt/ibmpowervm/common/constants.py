#
#
# =================================================================
# =================================================================

from nova.compute import power_state

# Power State information
NOSTATE = ''
RUNNING = 'Running'
STARTING = 'Starting'
SHUTDOWN = 'Not Activated'
MIGRATING_RUNNING = 'Migrating - Running'
MIGRATING_RUNNING_K2 = 'migrating running'
POWER_STATE = {
    NOSTATE: power_state.NOSTATE,
    RUNNING: power_state.RUNNING,
    SHUTDOWN: power_state.SHUTDOWN,
    STARTING: power_state.RUNNING,
    MIGRATING_RUNNING: power_state.RUNNING,
    MIGRATING_RUNNING_K2: power_state.RUNNING
}

# Image property used for denoting the device_name of the
# boot disk in volume_mapping
IMAGE_BOOT_DEVICE_PROPERTY = 'boot_device_name'


#
# Only these values should be returned in the 'stats' value of the
# Host.get_host_stats table.
#
COMPUTE_STATS = [
    'active_migrations_in_progress',
    'active_migrations_supported',
    'compatibility_modes',
    'disk_available',
    'disk_total',
    'disk_used',
    'host_memory_free',
    'host_memory_reserved',
    'host_memory_total',
    'inactive_migrations_in_progress',
    'inactive_migrations_supported',
    'lmb_size',
    'memory_mb_used',
    'num_vios_virtual_slots_in_use',
    'num_vios_virtual_slots_reserved',
    'preferred_active_migrations_supported',
    'preferred_inactive_migrations_supported',
    'proc_units',
    'proc_units_reserved',
    'proc_units_used',
    'source_host_for_migration',
    'vios_max_virtual_slots',
    'max_vcpus_per_aix_linux_lpar',
    'max_procs_per_aix_linux_lpar'
]

ACTIVE_LPAR_MOBILITY_CAPABLE = 'active_lpar_mobility_capable'
INACTIVE_LPAR_MOBILITY_CAPABLE = 'inactive_lpar_mobility_capable'
CUSTOM_MAC_ADDR_CAPABLE = 'custom_mac_addr_capable'
HYPERVISOR_STATE = 'hypervisor_state'
MANAGED_BY = 'managed_by'


def enum(**enums):
    return type('Enum', (), enums)

# enum type returned by getSystemType
PowerSystemType = enum(POWER5="POWER5", POWER6="POWER6", POWER7="POWER7",
                       POWER8="POWER8")

POWER8_MODE = 'POWER8'
POWER7_MODE = 'POWER7'
POWER6_PLUS_MODE = 'POWER6_Plus'
POWER6_MODE = 'POWER6'
POWER5_MODE = 'POWER5'

# Power VM hypervisor info
IBM_POWERVM_HYPERVISOR_TYPE = 'powervm'
IBM_POWERVM_HYPERVISOR_VERSION = '7.1'

# The types of LPARS that are supported.
POWERVM_SUPPORTED_INSTANCES = [['ppc64', 'powervm', 'hvm']]

# The supported storage connectivity types
CONNECTION_TYPE_NPIV = 'npiv'
CONNECTION_TYPE_SSP = 'ibm_ssp'
