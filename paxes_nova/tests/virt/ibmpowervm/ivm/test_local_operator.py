'''
Created on Mar 24, 2015

@author: root
'''
import mock
import testtools
from paxes_nova.virt.ibmpowervm.ivm import local_operator
from nova.openstack.common import processutils


# class BaseLocalOperatorTestCase(testtools.TestCase):
#     def setUp(self):
#         super(BaseLocalOperatorTestCase, self).setUp()
#         self.localOpt = local_operator.BaseLocalOperator()
# 
#     def tearDown(self):
#         super(BaseLocalOperatorTestCase, self).tearDown()
# 
#     @mock.patch('paxes_nova.virt.ibmpowervm.ivm.local_operator.BaseLocalOperator.run_command._exec_local_command')
#     def test_run_command(self, mock_exec_local_command):
#         cmd = 'lssyscfg -r lpar -F name'
#         conn = None
#         check_exit_code = True
#         mock_exec_local_command.return_value = (['06-052EA','paxes_devstack_test3',
#                                                  'instance-00000093','test-destack',
#                                                  'instance-000000e7', 'instance-000000e8',],
#                                                 ''
#                                                 )
#         self.assertEqual(self.localOpt.run_command(cmd, conn, check_exit_code), 
#                                                 (['06-052EA','paxes_devstack_test3',
#                                                  'instance-00000093','test-destack',
#                                                  'instance-000000e7', 'instance-000000e8',],
#                                                 ''
#                                                 ))


class IVMLocalOperatorTestCase(testtools.TestCase):
    def setUp(self):
        super(IVMLocalOperatorTestCase, self).setUp()
        self.ivm_loc_opt = local_operator.IVMLocalOperator()

    def tearDown(self):
        super(IVMLocalOperatorTestCase, self).tearDown()

    @mock.patch('nova.openstack.common.processutils.execute')
    def test_run_interactive(self, mock_processutils_execute):
        cmd = ['oem_setup_env', 'lsvg -o', 'exit']
        mock_processutils_execute.return_value = ('datavg\nrootvg\n', '')
        self.assertEqual(self.ivm_loc_opt.run_interactive(cmd), ['datavg', 'rootvg'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.local_operator.IVMLocalOperator.run_interactive')
    def test_get_wwpns(self, mock_run_interactive):
        mock_run_interactive.return_value = (['  fcs2             U78AB.001.WZSJH1H-P1-C3-T1  4Gb FC PCI Express Adapter (df1000fe)', '',
                                              '        Part Number.................00E0807',
                                              '        Serial Number...............1A249002C8',
                                              '        Manufacturer................001A',
                                              '        EC Level.................... D77162',
                                              '        Customer Card ID Number.....5774',
                                              '        Manufacturer................001',
                                              '        FRU Number.................. 00E0807',
                                              '        Device Specific.(ZM)........3',
                                              '        Network Address.............10000090FA1B2436',
                                              '        ROS Level and ID............02E8277F',
                                              '        Device Specific.(Z0)........2057706D',
                                              '        Device Specific.(Z1)........00000000',
                                              '        Device Specific.(Z2)........00000000',
                                              '        Device Specific.(Z3)........03000909',
                                              '        Device Specific.(Z4)........FFE01212',
                                              '        Device Specific.(Z5)........02E8277F',
                                              '        Device Specific.(Z6)........06E12715',
                                              '        Device Specific.(Z7)........07E1277F',
                                              '        Device Specific.(Z8)........20000090FA1B2436',
                                              '        Device Specific.(Z9)........ZS2.71X15',
                                              '        Device Specific.(ZA)........Z1F2.70A5 ',
                                              '        Device Specific.(ZB)........Z2F2.71X15',
                                              '        Device Specific.(ZC)........00000000',
                                              '        Hardware Location Code......U78AB.001.WZSJH1H-P1-C3-T1', '',
                                              '  fcs3             U78AB.001.WZSJH1H-P1-C3-T2  4Gb FC PCI Express Adapter (df1000fe)', '',
                                              '        Part Number.................00E0807',
                                              '        Serial Number...............1A249002C8',
                                              '        Manufacturer................001A',
                                              '        EC Level.................... D77162',
                                              '        Customer Card ID Number.....5774',
                                              '        Manufacturer................001',
                                              '        FRU Number.................. 00E0807',
                                              '        Device Specific.(ZM)........3',
                                              '        Network Address.............10000090FA1B2437',
                                              '        ROS Level and ID............02E8277F',
                                              '        Device Specific.(Z0)........2057706D',
                                              '        Device Specific.(Z1)........00000000',
                                              '        Device Specific.(Z2)........00000000',
                                              '        Device Specific.(Z3)........03000909',
                                              '        Device Specific.(Z4)........FFE01212',
                                              '        Device Specific.(Z5)........02E8277F',
                                              '        Device Specific.(Z6)........06E12715',
                                              '        Device Specific.(Z7)........07E1277F',
                                              '        Device Specific.(Z8)........20000090FA1B2437',
                                              '        Device Specific.(Z9)........ZS2.71X15', 
                                              '        Device Specific.(ZA)........Z1F2.70A5 ', 
                                              '        Device Specific.(ZB)........Z2F2.71X15',
                                              '        Device Specific.(ZC)........00000000',
                                              '        Hardware Location Code......U78AB.001.WZSJH1H-P1-C3-T2', '',
                                              '  fcs0             U78AB.001.WZSJH1H-P1-C2-T1  4Gb FC PCI Express Adapter (df1000fe)', '',
                                              '        Part Number.................00E0807',
                                              '        Serial Number...............1A2490024E',
                                              '        Manufacturer................001A',
                                              '        EC Level.................... D77162',
                                              '        Customer Card ID Number.....5774',
                                              '        Manufacturer................001',
                                              '        FRU Number.................. 00E0807',
                                              '        Device Specific.(ZM)........3',
                                              '        Network Address.............10000090FA1B2874',
                                              '        ROS Level and ID............02E8277F',
                                              '        Device Specific.(Z0)........2057706D',
                                              '        Device Specific.(Z1)........00000000',
                                              '        Device Specific.(Z2)........00000000',
                                              '        Device Specific.(Z3)........03000909',
                                              '        Device Specific.(Z4)........FFE01212',
                                              '        Device Specific.(Z5)........02E8277F',
                                              '        Device Specific.(Z6)........06E12715',
                                              '        Device Specific.(Z7)........07E1277F',
                                              '        Device Specific.(Z8)........20000090FA1B2874',
                                              '        Device Specific.(Z9)........ZS2.71X15',
                                              '        Device Specific.(ZA)........Z1F2.70A5 ',
                                              '        Device Specific.(ZB)........Z2F2.71X15',
                                              '        Device Specific.(ZC)........00000000',
                                              '        Hardware Location Code......U78AB.001.WZSJH1H-P1-C2-T1', '',
                                              '  fcs1             U78AB.001.WZSJH1H-P1-C2-T2  4Gb FC PCI Express Adapter (df1000fe)', '',
                                              '        Part Number.................00E0807',
                                              '        Serial Number...............1A2490024E',
                                              '        Manufacturer................001A',
                                              '        EC Level.................... D77162',
                                              '        Customer Card ID Number.....5774',
                                              '        Manufacturer................001',
                                              '        FRU Number.................. 00E0807',
                                              '        Device Specific.(ZM)........3',
                                              '        Network Address.............10000090FA1B2875',
                                              '        ROS Level and ID............02E8277F',
                                              '        Device Specific.(Z0)........2057706D',
                                              '        Device Specific.(Z1)........00000000',
                                              '        Device Specific.(Z2)........00000000',
                                              '        Device Specific.(Z3)........03000909',
                                              '        Device Specific.(Z4)........FFE01212',
                                              '        Device Specific.(Z5)........02E8277F',
                                              '        Device Specific.(Z6)........06E12715',
                                              '        Device Specific.(Z7)........07E1277F',
                                              '        Device Specific.(Z8)........20000090FA1B2875',
                                              '        Device Specific.(Z9)........ZS2.71X15',
                                              '        Device Specific.(ZA)........Z1F2.70A5 ',
                                              '        Device Specific.(ZB)........Z2F2.71X15',
                                              '        Device Specific.(ZC)........00000000',
                                              '        Hardware Location Code......U78AB.001.WZSJH1H-P1-C2-T2', '', '',
                                              '  PLATFORM SPECIFIC', '', '  Name:  fibre-channel',
                                              '    Model:  LPe11002', '    Node:  fibre-channel@0',
                                              '    Device Type:  fcp',
                                              '    Physical Location: U78AB.001.WZSJH1H-P1-C2-T1', '',
                                              '  Name:  fibre-channel', '    Model:  LPe11002',
                                              '    Node:  fibre-channel@0,1',
                                              '    Device Type:  fcp',
                                              '    Physical Location: U78AB.001.WZSJH1H-P1-C2-T2', '',
                                              '  Name:  fibre-channel',
                                              '    Model:  LPe11002',
                                              '    Node:  fibre-channel@0',
                                              '    Device Type:  fcp',
                                              '    Physical Location: U78AB.001.WZSJH1H-P1-C3-T1', '',
                                              '  Name:  fibre-channel',
                                              '    Model:  LPe11002',
                                              '    Node:  fibre-channel@0,1',
                                              '    Device Type:  fcp',
                                              '    Physical Location: U78AB.001.WZSJH1H-P1-C3-T2'])

        self.assertEqual(self.ivm_loc_opt.get_wwpns(), ['10000090FA1B2436',
                                                        '10000090FA1B2437',
                                                        '10000090FA1B2874',
                                                        '10000090FA1B2875'])
