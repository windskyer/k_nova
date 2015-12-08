'''
Created on Mar 24, 2015

@author: root
'''
import mock
import testtools
from paxes_nova.virt.ibmpowervm.ivm import operator
from paxes_nova.virt.ibmpowervm.ivm.common import Connection


class ISCSIIVMOperatorTestCase(testtools.TestCase):
    def setUp(self):
        super(ISCSIIVMOperatorTestCase, self).setUp()
        conn = Connection('172.24.23.140','padmin','padmin')
        self.opt_ISCSIDiskIVM = operator.ISCSIIVMOperator(conn)

    def tearDown(self):
        super(ISCSIIVMOperatorTestCase, self).tearDown()

    #@mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.ISCSIIVMOperator.set_iscsi_cfg')
    #def test_set_iscsi_cfg(self, mock_run_interactive):
    def testset_iscsi_cfg(self):
        data = {
            "target_iqn": "iqn.2010-10.org.openstack",
            "target_portal": "172.24.23.43:3260",
            "volume_id": "c6054706-41cc-446c-a2af-7538d5532ccd",
            "target_lun": 1,
        }
        value = ('172.24.23.43' '3260' 'iqn.2010-10.org.openstack')
        #data = ('172.24.23.43' '3260' 'iqn')

        #mock_run_interactive.return_value = (data)

        self.assertEqual(self.opt_ISCSIDiskIVM.set_iscsi_cfg(data), value)

    def testdel_iscsi_cfg(self):
        data = {
            "target_iqn": "iqn.2010-10.org.openstack",
            "target_portal": "172.24.23.43:3260",
            "volume_id": "c6054706-41cc-446c-a2af-7538d5532ccd",
            "target_lun": 1,
        }

        value = ('172.24.23.43' '3260' 'iqn.2010-10.org.openstack')
        self.assertEqual(self.opt_ISCSIDiskIVM.del_iscsi_cfg(data), None)

    def testget_disk_uid_by_name(self):
        data = 'hdisk37'
        value = 'iqn.1994-05.com.vios:26632222b8a1:volume-87e53cfc-0'
        self.assertEqual(self.opt_ISCSIDiskIVM.get_disk_uid_by_name(data), value)

