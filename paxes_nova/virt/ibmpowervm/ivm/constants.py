#
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import decimal

from nova.compute import power_state

# Power State information
IBM_POWERVM_NOSTATE = ''
IBM_POWERVM_RUNNING = 'Running'
IBM_POWERVM_STARTING = 'Starting'
IBM_POWERVM_SHUTDOWN = 'Not Activated'
IBM_POWERVM_MIGRATING_RUNNING = 'Migrating - Running'
IBM_POWERVM_POWER_STATE = {
    IBM_POWERVM_NOSTATE: power_state.NOSTATE,
    IBM_POWERVM_RUNNING: power_state.RUNNING,
    IBM_POWERVM_SHUTDOWN: power_state.SHUTDOWN,
    IBM_POWERVM_STARTING: power_state.RUNNING,
    IBM_POWERVM_MIGRATING_RUNNING: power_state.RUNNING
}

POWERVM_MIN_ROOT_GB = 3
# Resource Constants
POWERVM_MEM_BUFFER = 512
# Smallest value the hypervisor allows for processing units
POWERVM_MIN_PROC_UNITS = decimal.Decimal('.1')

LIVE_MIGRATE_DATA_DIR = '/home/padmin/'
SVC_KEY_VDISK_NAME = 'name'

# Dictionary for calculating maximum memory while
# booting or resiziing an instance
MAX_MEM_MULTIPLIER_DICT = {(float("-inf"), 1023): 8,
                           (1024, 4095): 4, (4096, float("inf")): 2}

# Conection time our for ssh connection
IBMPOWERVM_CONNECTION_TIMEOUT = 60

# Power VM CPU info
IBM_POWERVM_CPU_INFO = 'powervm'

# Memory
IBM_POWERVM_MIN_MEM = 512
IBM_POWERVM_MAX_MEM = 1024
# CPU
IBM_POWERVM_MAX_CPUS = 1
IBM_POWERVM_MIN_CPUS = 1
