'''
Created on Apr 20, 2015

@author: root
'''


import mock
import testtools
from paxes_nova.virt.ibmpowervm.ivm import operator
from paxes_nova.virt.ibmpowervm.ivm.common import Connection
from paxes_nova.virt.ibmpowervm.ivm import exception
from decimal import Decimal
from paxes_nova.virt.ibmpowervm.ivm.operator import IVMOperator


class IVMOperatorTestCase(testtools.TestCase):
    def setUp(self):
        super(IVMOperatorTestCase, self).setUp()
        conn = Connection('172.24.23.212', 'root', 'teamsun')
        self.ivm_opt = operator.IVMOperator(conn)
        self.rasis = exception

    def tearDown(self):
        super(IVMOperatorTestCase, self).tearDown()

    @mock.patch('nova.openstack.common.processutils.execute')
    def test_run_interactive(self, mock_processutils_execute):
        cmd = ['oem_setup_env', 'lsvg -o', 'exit']
        mock_processutils_execute.return_value = ('datavg\nrootvg\n', '')
        self.assertEqual(self.ivm_opt.run_interactive(cmd), [])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_interactive')
    def test_get_wwpns(self, mock_run_interactive):
        mock_run_interactive.return_value = (['        Network Address.............10000090FA1B2436',
                                              '        Network Address.............10000090FA1B2437',
                                              '        Network Address.............10000090FA1B2874',
                                              '        Network Address.............10000090FA1B2875',
                                              ])

        self.assertEqual(self.ivm_opt.get_wwpns(), ['10000090FA1B2436',
                                                    '10000090FA1B2437',
                                                    '10000090FA1B2874',
                                                    '10000090FA1B2875',
                                                    ])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_interactive')
    def test_get_device_name_by_wwpn(self, mock_run_interactive):
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
        wwpn = '10000090FA1B2436'
        self.assertEqual(self.ivm_opt.get_device_name_by_wwpn(wwpn), 'fcs2')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_vopt_size(self, mock_run_command):
        mock_run_command.return_value = ('6515')
        self.assertEqual(self.ivm_opt.get_vopt_size(), '6515')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_staging_size(self, mock_run_command):
        mock_run_command.return_value = (['9613688'])
        self.assertEqual(self.ivm_opt.get_vopt_size(), ['9613688'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_actual_lpar_name')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_vios_command')
    def test_get_lpar_mem(self, mock_run_vios_command, mock_get_actual_lpar_name):
        lpar_name = 'instane-0000011f'
        mock_get_actual_lpar_name.return_value = ('instane-0000011f')
        mock_run_vios_command.return_value = (['lpar_name=instane-0000011f',
                                               'lpar_id=26',
                                               'mem_mode=ded',
                                               'curr_min_mem=512',
                                               'curr_mem=1024',
                                               'curr_max_mem=4096',
                                               'pend_min_mem=512',
                                               'pend_mem=1024',
                                               'pend_max_mem=4096',
                                               'run_min_mem=0',
                                               'run_mem=1024'])
        self.assertEqual(self.ivm_opt.get_lpar_mem(lpar_name), {'lpar_name': 'instane-0000011f'})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_vios_command')
    def test_get_lpar_proc(self, mock_run_vios_command):
        lpar_name = 'instane-0000011f'
        mock_run_vios_command.return_value = (['lpar_name=instane-0000011f',
                                               'lpar_id=26',
                                               'curr_shared_proc_pool_id=0',
                                               'curr_proc_mode=shared',
                                               ])
        self.assertEqual(self.ivm_opt.get_lpar_proc(lpar_name), {'lpar_name': 'instane-0000011f'})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_lpar_cpu_util(self, mock_run_command):
        lpar_id = 26
        mock_run_command.return_value = (['128,1,1024',
                                          '128,1,1024',
                                          '128,1,1024'
                                          ])
        self.assertEqual(self.ivm_opt.get_lpar_cpu_util(lpar_id), 0)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_cpu_info_for_lpars(self, mock_run_command):
        mock_run_command.return_value = (['instance-00000119,shared,0.50,1',
                                          'instance-00000121,shared,0.20,1',
                                          'instance-0000015b,shared,0.50,1',
                                          'instance-00000001,shared,0.50,1'])
        self.assertEqual(self.ivm_opt.get_cpu_info_for_lpars(), {'instance-00000119': Decimal('0.50'),
                                                                 'instance-00000121': Decimal('0.20'),
                                                                 'instance-0000015b': Decimal('0.50'),
                                                                 'instance-00000001': Decimal('0.50')
                                                                 })

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator._calc_cpu_utilization')
    def test_get_lpar_info(self, mock_calc_cpu_utilization, mock_run_command):
        lpar_id = 26
        mock_run_command.return_value = ['entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330',
                                         'entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330',
                                         'entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330']
        mock_calc_cpu_utilization.return_value = (0)
        self.assertEqual(self.ivm_opt.get_lpar_info(lpar_id), {'capped_cycles': '317971615126',
                                                               'curr_cpu_util': '0.0',
                                                               'entitled_cycles': '283424875907440',
                                                               'uncapped_cycles': '18638892330'})

#     @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
#     @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator._calc_cpu_utilization')
#     def test_get_lpar_info_fault(self, mock_calc_cpu_utilization, mock_run_command):
#         lpar_id = 26
#         ex_args = {'command': 'lslparutil --filter "lpar_ids=26" -n 3 -r lpar',
#                     'error': ['entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330',
#                               'entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330']
#                    }
#         mock_run_command.return_value = ['entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330',
#                                          'entitled_cycles=283424875907440,capped_cycles=317971615126,uncapped_cycles=18638892330']
#         mock_calc_cpu_utilization.return_value = (0)
#         self.assertRaises(self.rasis.IBMPowerVMCommandFailed, self.ivm_opt.get_lpar_info(lpar_id), ex_args)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_disk_names_for_vhost(self, mock_run_command):
        vhost = 'vhost1'
        mock_run_command.return_value = (['0x8200000000000000:/var/vio/VMLibrary/c53b6b58-4eca-8e90-c3e9e0f0babb:0x8100000000000000:lv16'])
        self.assertEqual(self.ivm_opt.get_disk_names_for_vhost(vhost, local=True), ['lv16'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_hdisk_reserve_policy(self, mock_run_command):
        diskname = 'hdisk4'
        mock_run_command.return_value = (['value', ' ', 'no_reserve'])
        self.assertEqual(self.ivm_opt.get_hdisk_reserve_policy(diskname), 'no_reserve')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_management_sys_name(self, mock_run_command):
        mock_run_command.return_value = (['Server-8246-L2D-SN06052EA'])
        self.assertEqual(self.ivm_opt.get_management_sys_name(), 'Server-8246-L2D-SN06052EA')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_actual_lpar_name')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_refcode(self, mock_run_command, mock_get_actual_lpar_name):
        instance_name = 'instance-0000011f'
        mock_run_command.return_value = (['Linux ppc64,04/09/2015 10:44:57'])
        mock_get_actual_lpar_name.return_value = ('instance-0000011f')
        self.assertEqual(self.ivm_opt.get_refcode(instance_name), 'Linux ppc64')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_actual_lpar_name')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_live_migration_state(self, mock_run_command, mock_get_actual_lpar_name):
        inst_name = 'instance-0000011f'
        mock_run_command.return_value = (['Not Migrating'])
        mock_get_actual_lpar_name.return_value = ('instance-0000011f')
        self.assertEqual(self.ivm_opt.get_live_migration_state(inst_name), 'Not Migrating')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_lpar_proc_compat_modes(self, mock_run_command):
        mock_run_command.return_value = (['"default,POWER6,POWER6+,POWER7"'])
        self.assertEqual(self.ivm_opt.get_lpar_proc_compat_modes(), ['default', 'POWER6', 'POWER6+', 'POWER7'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_actual_lpar_name')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_vios_command')
    def test_get_curr_and_desired_proc_compat_modes(self, mock_run_vios_command, mock_get_actual_lpar_name):
        instance_name = 'instance-0000011f'
        mock_run_vios_command.return_value = (['POWER7,default'])
        mock_get_actual_lpar_name.return_value = ('instance-0000011f')
        self.assertEqual(self.ivm_opt.get_curr_and_desired_proc_compat_modes(instance_name), ['POWER7', 'default'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_actual_lpar_name')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_vios_command')
    def test_get_lpar_operating_system(self, mock_run_vios_command, mock_get_actual_lpar_name):
        instance_name = 'instance-0000011f'
        mock_run_vios_command.return_value = (['0.0.0.0.0.0'])
        mock_get_actual_lpar_name.return_value = ('instance-0000011f')
        self.assertEqual(self.ivm_opt.get_curr_and_desired_proc_compat_modes(instance_name), ['0.0.0.0.0.0'])

    def test_get_disk_names_for_vhost_frm_dict(self):
        vhost_id = 3
        disk_dict = {1:'15',
                     2:'80',
                     3:'123'}
        self.assertEqual(self.ivm_opt.get_disk_names_for_vhost_frm_dict(vhost_id, disk_dict), [])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_disk_uid_by_name(self, mock_run_command):
        disk_name = 'hdisk3'
        mock_run_command.return_value = (['332136005076300818001A000000000000D4F04214503IBMfcp'])
        self.assertEqual(self.ivm_opt.get_disk_uid_by_name(disk_name), '6005076300818001A000000000000D4F')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_disk_names_for_vhost_frm_dict')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_disk_uid_by_name')
    def test_get_volumes_by_vhost_from_dict(self, mock_get_disk_uid_by_name, mock_get_disk_names_for_vhost_frm_dict):
        vhost_id = 3
        disk_dict = {1:'15',
                     2:'80',
                     3:'123'}
        mock_get_disk_uid_by_name.return_value = ('6005076300818001A000000000000D4F')
        mock_get_disk_names_for_vhost_frm_dict.return_value = ([])
        self.assertEqual(self.ivm_opt.get_volumes_by_vhost_from_dict(vhost_id, disk_dict), [])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_vios_command')
    def test_get_vhost_by_instance_id(self, mock_run_vios_command):
        instance_id = 17
        mock_run_vios_command.return_value = (['vhost15'])
        self.assertEqual(self.ivm_opt.get_vhost_by_instance_id(instance_id), 'vhost15')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator._get_all_virt_slots_in_use')
    def test_get_num_reserved_in_use_vios_slots(self, mock__get_all_virt_slots_in_use, mock_run_command):
        managed_lpar_names = ['06-052EA', '06-052EB']
        mock_run_command.return_value = (['0,serial', '1,serial', '2,scsi', '3,reserved', '32,eth'])
        mock__get_all_virt_slots_in_use.return_value = (1)
        self.assertEqual(self.ivm_opt.get_num_reserved_in_use_vios_slots(managed_lpar_names), (4, 1))

    def test_get_volume_aix_conn_info(self):
        volume_data = {'target_wwn': ['10000090FA1B2436',
                                      '10000090FA1B2437',
                                      '10000090FA1B2874',
                                      '10000090FA1B2875'],
                       'target_lun': '10'}
        self.assertEqual(self.ivm_opt.get_volume_aix_conn_info(volume_data), ['10000090fa1b2436,a000000000000',
                                                                              '10000090fa1b2437,a000000000000',
                                                                              '10000090fa1b2874,a000000000000',
                                                                              '10000090fa1b2875,a000000000000'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_devname_by_aix_conn(self, mock_run_command):
        conn_info = ['5005076803080067,2000000000000']
        mock_run_command.return_value = (['Enabled:hdisk4:fscsi2:5005076803080067,2000000000000'])
        self.assertEqual(self.ivm_opt.get_devname_by_aix_conn(conn_info), {'device_name': 'hdisk4'})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_fcs_parent_devices(self, mock_run_command):
        mock_run_command.return_value = (['pci4:fcs0', 'pci4:fcs1', 'pci5:fcs2', 'pci5:fcs3'])
        self.assertEqual(self.ivm_opt.get_fcs_parent_devices(), {'pci4': ['fcs0', 'fcs1'], 'pci5': ['fcs2', 'fcs3']})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_interactive')
    def test_get_fcs_device_names(self, mock_run_interactive):
        wwpns = ['10000090FA1B2436',
                 '10000090FA1B2437',
                 '10000090FA1B2874',
                 '10000090FA1B2875']
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
        self.assertEqual(self.ivm_opt.get_fcs_device_names(wwpns), ['fcs2', 'fcs3', 'fcs0', 'fcs1'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_disk_name_by_volume_uid(self, mock_run_command):
        uid = 'D5304214503'
        mock_run_command.return_value = (['hdisk4:332136005076300818001A000000000000D5304214503IBMfcp'])
        self.assertEqual(self.ivm_opt.get_disk_name_by_volume_uid(uid), 'hdisk4')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_lpar_max_virtual_slots(self, mock_run_command):
        lpar_id = '26'
        mock_run_command.return_value = ([64])
        self.assertEqual(self.ivm_opt.get_lpar_max_virtual_slots(lpar_id), 64)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_lpar_max_virtual_slots')
    def test_get_vios_max_virt_slots(self, mock_get_lpar_max_virtual_slots):
        mock_get_lpar_max_virtual_slots.return_value = (64)
        self.assertEqual(self.ivm_opt.get_vios_max_virt_slots(), 64)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_hyp_capability(self, mock_run_command):
        mock_run_command.return_value = (["active_lpar_mobility_capable,inactive_lpar_mobility_capable,cod_proc_capable,vet_activation_capable,shared_proc_capable,active_lpar_share_idle_procs_capable,micro_lpar_capable,dlpar_mem_capable,assign_phys_io_capable,lpar_avail_priority_capable,lpar_proc_compat_mode_capable,virtual_fc_capable,active_mem_sharing_capable"])
        self.assertEqual(self.ivm_opt.get_hyp_capability(), {'active_lpar_mobility_capable': True, 'inactive_lpar_mobility_capable': True})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_migration_stats(self, mock_run_command):
        mock_run_command.return_value = (['64,8,0,0'])
        self.assertEqual(self.ivm_opt.get_migration_stats(), {'inactv_migr_sup': 64,
                                                              'actv_migr_supp': 8,
                                                              'inactv_migr_prg': 0,
                                                              'actv_migr_prg': 0})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_inactv_migration_stats(self, mock_run_command):
        mock_run_command.return_value = (['64,0'])
        self.assertEqual(self.ivm_opt.get_inactv_migration_stats(), {'inactv_migr_sup': 64,
                                                                     'inactv_migr_prg': 0})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_get_actv_migration_stats(self, mock_run_command):
        mock_run_command.return_value = (['8,0'])
        self.assertEqual(self.ivm_opt.get_actv_migration_stats(), {'actv_migr_supp': 8,
                                                                   'actv_migr_prg': 0})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_check_vopt_exists(self, mock_run_command):
        name = 'RHEL6.5-2013-Server-ppc64-DVD.iso'
        mock_run_command.return_value = (['10d2f623-4225-45ec-bc46-4b98ab7c65b3',
                                          '61702c3b-001b-4311-850e-cab2f016add1',
                                          '6c0ec0e7-7655-4932-bcca-cb30a6356fab',
                                          '6df387ae-6be8-40b8-a1d2-6d3633a0fc24',
                                          '753ce55f-4983-4e7e-99c0-ce7e497f559f',
                                          '8354e5a1-91dd-47fd-b3a0-7cf8f057cb43',
                                          '8354e5a1-91dd-47fd-b3a0-7cf8f057cb43',
                                          'RHEL-7.0-20140507.0-Server-ppc64-dvd1.iso',
                                          'RHEL6.4-20130130.0-Server-ppc64-DVD1.',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'RHEL6.5-2013-Server-ppc64-DVD.iso',
                                          'b3ec3547-307d-4a58-9c69-4955f6df2059',
                                          'c53b6b58-e6f3-4eca-8e90-c3e9e0f0babb',
                                          'd0ed1883-2812-4513-b3f5-092ee8adecd3',
                                          'e4efe10b-34e6-4bb2-b139-0d9a54e55456',
                                          'fdf9b77e-e62c-4fc6-a52a-c355217adaea',
                                          'fdf9b77e-e62c-4fc6-a52a-c355217adaea',
                                          'vopt_06a5f3344fd1402d9bf5bc2c2a5bff41',
                                          'vopt_1e4ea2dd2afa46e89fd0a3234336157e',
                                          'vopt_2a37ccafa8f343a2a08ef119d7b7513b',
                                          'vopt_36e1d56b897946d78293a4594362b884',
                                          'vopt_53de1f8e17d44c1395c4c9b3a4d603fe',
                                          'vopt_59e46a072d484fd9a8c5cdded8214aa4',
                                          'vopt_5e6b50fa0c7e4befa2a537706544e07b',
                                          'vopt_60056ead188a447fbbf51f0dc416627f',
                                          'vopt_6490f79f8e2049018e817fdb75a2cc79',
                                          'vopt_6572e1870a9a4b4993f34aae5d351e4d',
                                          'vopt_7c3085768c9e4a0ab796fa7148b58824',
                                          'vopt_83f111feb8464f5aa0e2702d9cad54ae',
                                          'vopt_c1e896b6656f4c94a70036ae4c518795',
                                          'vopt_d0fd2eed31034148a54659b2987e5ded',
                                          ])
        self.assertEqual(self.ivm_opt.check_vopt_exists(name), True)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_vios_command')
    def test_prepare_vhost_dev_lun_dict(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['vhost0:0x00000002:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp2vd2',
                                               'vhost1:0x00000003:0x8200000000000000: :0x8100000000000000:lv02',
                                               'vhost2:0x00000004:0x8200000000000000: :0x8100000000000000:lv00',
                                               'vhost3:0x00000005:0x8200000000000000:/var/vio/VMLibrary/RHEL6.4-20130130.0-Server-ppc64-DVD1.:0x8100000000000000:lv01',
                                               'vhost4:0x00000006:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lv04:0x8300000000000000:lv04_ext',
                                               'vhost5:0x00000007:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lv05',
                                               'vhost6:0x0000000f:0x8200000000000000:/var/vio/VMLibrary/dc5e181d-797d-4e89-8b72-05e6d87519d5:0x8100000000000000:lv20',
                                               'vhost7:0x00000008:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp8vd1',
                                               'vhost8:0x0000000a:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp10vd1',
                                               'vhost10:0x0000000b:0x8200000000000000:/var/vio/VMLibrary/vopt_83f111feb8464f5aa0e2702d9cad54ae:0x8100000000000000:lv03',
                                               'vhost11:0x0000000d:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp13vd1',
                                               'vhost12:0x0000000c:0x8200000000000000:/var/vio/VMLibrary/vopt_d0fd2eed31034148a54659b2987e5ded:0x8100000000000000:lv09',
                                               'vhost13:0x0000000e:0x8200000000000000:/var/vio/VMLibrary/8354e5a1-91dd-47fd-b3a0-7cf8f057cb43:0x8100000000000000:lv14',
                                               'vhost14:0x00000010:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp16vd1',
                                               'vhost15:0x00000011:0x8200000000000000:/var/vio/VMLibrary/e4efe10b-34e6-4bb2-b139-0d9a54e55456:0x8100000000000000:lv11',
                                               'vhost16:0x00000012:0x8200000000000000:/var/vio/VMLibrary/6c0ec0e7-7655-4932-bcca-cb30a6356fab:0x8100000000000000:lv10',
                                               'vhost17:0x00000013:0x8200000000000000:/var/vio/VMLibrary/8354e5a1-91dd-47fd-b3a0-7cf8f057cb43:0x8100000000000000:lv13',
                                               'vhost18:0x00000014:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp20vd1',
                                               'vhost19:0x00000015:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lp21vd1',
                                               'vhost21:0x00000017:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:test-paxes',
                                               'vhost22:0x00000018:0x8200000000000000:/var/vio/VMLibrary/b0d6fcc9-85ee-464e-af68-afdb921701af:0x8100000000000000:lv12',
                                               'vhost23:0x00000019:0x8200000000000000:/var/vio/VMLibrary/RHEL-7.0-20140507.0-Server-ppc64-dvd1.iso:0x8100000000000000:lp25vd1',
                                               'vhost24:0x0000001a:0x8200000000000000:/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso:0x8100000000000000:lv06',
                                               'vhost25:0x0000001b:0x8200000000000000:/var/vio/VMLibrary/c53b6b58-e6f3-4eca-8e90-c3e9e0f0babb:0x8100000000000000:lv16',
                                               'vhost26:0x0000001c:0x8100000000000000:/var/vio/VMLibrary/fdf9b77e-e62c-4fc6-a52a-c355217adaea:0x8200000000000000:hdisk3',
                                               'vhost27:0x0000001d:0x8100000000000000:/var/vio/VMLibrary/fdf9b77e-e62c-4fc6-a52a-c355217adaea:0x8200000000000000:hdisk4',
                                               ])
        self.assertEqual(self.ivm_opt.prepare_vhost_dev_lun_dict(), ({'vhost0': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp2vd2'],
                                                                     'vhost1': ['0x8200000000000000', ' ', '0x8100000000000000', 'lv02'],
                                                                     'vhost10': ['0x8200000000000000', '/var/vio/VMLibrary/vopt_83f111feb8464f5aa0e2702d9cad54ae', '0x8100000000000000', 'lv03'],
                                                                     'vhost11': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp13vd1'],
                                                                     'vhost12': ['0x8200000000000000', '/var/vio/VMLibrary/vopt_d0fd2eed31034148a54659b2987e5ded', '0x8100000000000000', 'lv09'],
                                                                     'vhost13': ['0x8200000000000000', '/var/vio/VMLibrary/8354e5a1-91dd-47fd-b3a0-7cf8f057cb43', '0x8100000000000000', 'lv14'],
                                                                     'vhost14': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp16vd1'],
                                                                     'vhost15': ['0x8200000000000000', '/var/vio/VMLibrary/e4efe10b-34e6-4bb2-b139-0d9a54e55456', '0x8100000000000000', 'lv11'],
                                                                     'vhost16': ['0x8200000000000000', '/var/vio/VMLibrary/6c0ec0e7-7655-4932-bcca-cb30a6356fab', '0x8100000000000000', 'lv10'],
                                                                     'vhost17': ['0x8200000000000000', '/var/vio/VMLibrary/8354e5a1-91dd-47fd-b3a0-7cf8f057cb43', '0x8100000000000000', 'lv13'],
                                                                     'vhost18': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp20vd1'],
                                                                     'vhost19': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp21vd1'],
                                                                     'vhost2': ['0x8200000000000000', ' ', '0x8100000000000000', 'lv00'],
                                                                     'vhost21': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'test-paxes'],
                                                                     'vhost22': ['0x8200000000000000', '/var/vio/VMLibrary/b0d6fcc9-85ee-464e-af68-afdb921701af', '0x8100000000000000', 'lv12'],
                                                                     'vhost23': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL-7.0-20140507.0-Server-ppc64-dvd1.iso', '0x8100000000000000', 'lp25vd1'],
                                                                     'vhost24': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lv06'],
                                                                     'vhost25': ['0x8200000000000000', '/var/vio/VMLibrary/c53b6b58-e6f3-4eca-8e90-c3e9e0f0babb', '0x8100000000000000', 'lv16'],
                                                                     'vhost26': ['0x8100000000000000', '/var/vio/VMLibrary/fdf9b77e-e62c-4fc6-a52a-c355217adaea', '0x8200000000000000', 'hdisk3'],
                                                                     'vhost27': ['0x8100000000000000', '/var/vio/VMLibrary/fdf9b77e-e62c-4fc6-a52a-c355217adaea', '0x8200000000000000', 'hdisk4'],
                                                                     'vhost3': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.4-20130130.0-Server-ppc64-DVD1.', '0x8100000000000000', 'lv01'],
                                                                     'vhost4': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lv04', '0x8300000000000000', 'lv04_ext'],
                                                                     'vhost5': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lv05'],
                                                                     'vhost6': ['0x8200000000000000', '/var/vio/VMLibrary/dc5e181d-797d-4e89-8b72-05e6d87519d5', '0x8100000000000000', 'lv20'],
                                                                     'vhost7': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp8vd1'],
                                                                     'vhost8': ['0x8200000000000000', '/var/vio/VMLibrary/RHEL6.5-2013-Server-ppc64-DVD.iso', '0x8100000000000000', 'lp10vd1']},
                                                                    {'0x00000002': 'vhost0',
                                                                     '0x00000003': 'vhost1',
                                                                     '0x00000004': 'vhost2',
                                                                     '0x00000005': 'vhost3',
                                                                     '0x00000006': 'vhost4',
                                                                     '0x00000007': 'vhost5',
                                                                     '0x00000008': 'vhost7',
                                                                     '0x0000000a': 'vhost8',
                                                                     '0x0000000b': 'vhost10',
                                                                     '0x0000000c': 'vhost12',
                                                                     '0x0000000d': 'vhost11',
                                                                     '0x0000000e': 'vhost13',
                                                                     '0x0000000f': 'vhost6',
                                                                     '0x00000010': 'vhost14',
                                                                     '0x00000011': 'vhost15',
                                                                     '0x00000012': 'vhost16',
                                                                     '0x00000013': 'vhost17',
                                                                     '0x00000014': 'vhost18',
                                                                     '0x00000015': 'vhost19',
                                                                     '0x00000017': 'vhost21',
                                                                     '0x00000018': 'vhost22',
                                                                     '0x00000019': 'vhost23',
                                                                     '0x0000001a': 'vhost24',
                                                                     '0x0000001b': 'vhost25',
                                                                     '0x0000001c': 'vhost26',
                                                                     '0x0000001d': 'vhost27'}))

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_command')
    def test_check_dlpar_connectivity(self, mock_run_command):
        instance_name = 'instance-0000011f'
        mock_run_command.return_value = (['0,0,none'])
        self.assertEqual(self.ivm_opt.check_dlpar_connectivity(instance_name), (True, 'none'))
        
        
        
        
        
        
        
        
        