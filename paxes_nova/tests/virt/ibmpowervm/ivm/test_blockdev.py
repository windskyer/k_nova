'''
Created on Mar 5, 2015

@author: root
'''
import mock
import testtools

from mock import patch
from mock import call

from paxes_nova.virt.ibmpowervm.ivm import common
from paxes_nova.virt.ibmpowervm.ivm import blockdev
from paxes_nova.virt.ibmpowervm.ivm import operator
from nova.compute.manager import ComputeVirtAPI


class PowerVMLocalVolumeAdapterTestCase(testtools.TestCase):
    def setUp(self):
        super(PowerVMLocalVolumeAdapterTestCase, self).setUp()
        self.pvm_con = common.Connection('127.0.0.1',
                                         'padmin',
                                         'padmin')
        self.virtapi = ComputeVirtAPI(self)
        self._ld_ivm_operator = operator.LocalDiskIVMOperator(self.pvm_con, self.virtapi)
        self._disk_adapter = blockdev.\
            PowerVMLocalVolumeAdapter(self.pvm_con,
                                      self._ld_ivm_operator)

    def tearDown(self):
        super(PowerVMLocalVolumeAdapterTestCase, self).tearDown()

    def test_get_vg_info(self):
        pass

    def test_create_logical_volume(self):
        pass
