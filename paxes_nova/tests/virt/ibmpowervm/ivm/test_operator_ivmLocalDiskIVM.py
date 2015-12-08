'''
Created on Mar 24, 2015

@author: root
'''
import mock
import testtools
from paxes_nova.virt.ibmpowervm.ivm import operator
from paxes_nova.virt.ibmpowervm.ivm.common import Connection


class LocalDiskIVMOperatorTestCase(testtools.TestCase):
    def setUp(self):
        super(LocalDiskIVMOperatorTestCase, self).setUp()
        conn = Connection('172.24.23.212','root','teamsun')
        self.opt_locDiskIVM = operator.LocalDiskIVMOperator(conn)

    def tearDown(self):
        super(LocalDiskIVMOperatorTestCase, self).tearDown()

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.LocalDiskIVMOperator.get_disk_name_by_vhost')
    def test_get_disk_name_by_vhost(self, mock_run_interactive):
        mock_run_interactive.return_value = (['/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2:hdisk11'])

        self.assertEqual(self.opt_locDiskIVM.get_disk_name_by_vhost(), ['hdisk11'])
