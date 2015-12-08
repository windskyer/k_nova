'''
Created on Mar 24, 2015

@author: root
'''
import mock
import re
import testtools
from paxes_nova.virt.ibmpowervm.ivm import operator
from paxes_nova.virt.ibmpowervm.ivm.common import Connection
from paxes_nova.virt.ibmpowervm.ivm import command
from paxes_nova.virt.ibmpowervm.ivm import lpar as LPAR


class BaseOperatorTestCase(testtools.TestCase):
    def setUp(self):
        super(BaseOperatorTestCase, self).setUp()
        conn = Connection('172.24.23.31','root','teamsun')
        self.opt_ivm = operator.IVMOperator(conn)
        self.opt_base = operator.BaseOperator(conn)
        setattr(self.opt_base, 'command', command.IVMCommand())
#         setattr(self.opt_base, 'run_interactive', self.opt_ivm.run_interactive())

    def tearDown(self):
        super(BaseOperatorTestCase, self).tearDown()

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_run_vios_command(self, mock_run_command):
        command = ['lssyscfg -r lpar -F name']
        mock_run_command.return_value = (['06-09FDA',
                                                'instance-00000011',
                                                'instance-00000004',
                                                'vm_test_import',
                                                'instance-00000002'])
        self.assertEqual(self.opt_base.run_vios_command(command), ['06-09FDA',
                                                            'instance-00000011',
                                                            'instance-00000004',
                                                            'vm_test_import',
                                                            'instance-00000002'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_list_lpar_instances(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['06-09FDA',
                                                'instance-00000011',
                                                'instance-00000004',
                                                'vm_test_import',
                                                'instance-00000012',
                                                'instance-00000008',
                                                'instance-00000001',
                                                'instance-00000002'])
 
        self.assertEqual(self.opt_base.list_lpar_instances(), ['06-09FDA',
                                                'instance-00000011',
                                                'instance-00000004',
                                                'vm_test_import',
                                                'instance-00000012',
                                                'instance-00000008',
                                                'instance-00000001',
                                                'instance-00000002'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_vcpu_info(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['curr_procs=8,pend_procs=8,pend_proc_units=1.00'])
        lpar_name = '06-09FDA'
        self.assertEqual(self.opt_ivm.get_vcpu_info(lpar_name), {'curr_procs': 'curr_procs=8',
                                                          'pend_procs': 'pend_procs=8',
                                                          'pend_proc_units': 'pend_proc_units=1.00'})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_memory_info_lpar(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['run_mem=4096,pend_mem=4096'])
        lpar_name = '06-09FDA'
        self.assertEqual(self.opt_base.get_memory_info_lpar(lpar_name), {'curr_mem': 'run_mem=4096',
                                                          'pend_mem': 'pend_mem=4096',})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_wwname_by_disk(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['value\n','\n',
                                               '5000cca016a7b29c'])
        disk_name = 'hdisk0'
        self.assertEqual(self.opt_base.get_wwname_by_disk(disk_name), '5000cca016a7b29c')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_stor_type_of_disk(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['parent\n','\n',
                                               'sas0'])
        disk_name = 'hdisk0'
        self.assertEqual(self.opt_base.get_stor_type_of_disk(disk_name), 'sas0')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_disk_of_lv(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['lv02:N/A\n','PV\n','hdisk1'])
        disk_name = 'hdisk0'
        self.assertEqual(self.opt_base.get_disk_of_lv(disk_name), 'hdisk1')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_disk_uid_by_name(self, mock_run_command):
        mock_run_command.return_value = (['332136005076300818001A000000000000CDC04214503IBMfcp'])
        disk_name = 'hdisk3'
        self.assertEqual(self.opt_base.get_disk_uid_by_name(disk_name), '6005076300818001A000000000000CDC')

#     @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.get_disk_names_for_vhost_frm_dict')
#     @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_interactive')
#     def test_get_root_gb_disk_from_dict(self, mock_run_vios_command):
#         mock_run_vios_command.return_value = (['332136005076300818001A000000000000CDC04214503IBMfcp'])
#         vhost_id = 'hdisk0'
#         disk_dict = ''
#         self.assertEqual(self.opt_ivm.get_root_gb_disk_from_dict(vhost_id,disk_dict), None)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_vscsi_storage_info(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['0x00000002:vtopt1:/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2:0x8100000000000000:vtscsi1:hdisk11:0x8200000000000000\n'
                                              ,'0x00000003:vtscsi3:lv02:0x8100000000000000\n'
                                              ,'0x00000004:vtopt2: :0x8200000000000000:vtscsi4:lp4vd1:0x8100000000000000\n'
                                              ,'0x00000005:vtopt3:/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2:0x8100000000000000:vtscsi2:hdisk12:0x8200000000000000\n'
                                              ,'0x00000006: : :\n'
                                              ,'0x00000009: : :\n'
                                              ,'0x0000000a:vtopt0:/var/vio/VMLibrary/0270c6c4-d5ba-4004-9e1e-7158fa8fd0fe:0x8200000000000000:vtscsi0:lv00:0x8100000000000000\n'])
        self.assertEqual(self.opt_base.get_vscsi_storage_info(), {'0x00000002': ['vtopt1',
                                                                                    '/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2',
                                                                                    '0x8100000000000000',
                                                                                    'vtscsi1',
                                                                                    'hdisk11',
                                                                                    '0x8200000000000000\n'],
                                                                     '0x00000003': ['vtscsi3', 'lv02', '0x8100000000000000\n'],
                                                                     '0x00000004': ['vtopt2',
                                                                                    ' ',
                                                                                    '0x8200000000000000',
                                                                                    'vtscsi4',
                                                                                    'lp4vd1',
                                                                                    '0x8100000000000000\n'],
                                                                     '0x00000005': ['vtopt3',
                                                                                    '/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2',
                                                                                    '0x8100000000000000',
                                                                                    'vtscsi2',
                                                                                    'hdisk12',
                                                                                    '0x8200000000000000\n'],
                                                                     '0x00000006': [' ', ' ', '\n'],
                                                                     '0x00000009': [' ', ' ', '\n'],
                                                                     '0x0000000a': ['vtopt0',
                                                                                    '/var/vio/VMLibrary/0270c6c4-d5ba-4004-9e1e-7158fa8fd0fe',
                                                                                    '0x8200000000000000',
                                                                                    'vtscsi0',
                                                                                    'lv00',
                                                                                    '0x8100000000000000\n']})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_npiv_storage_info(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (None)
        self.assertEqual(self.opt_base.get_npiv_storage_info(), None)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_misc_lpar_info(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['1,0\n'
                                            ,'2,0\n'
                                            ,'3,0\n'
                                            ,'4,0\n'
                                            ,'5,0\n'
                                            ,'6,0\n'])
        self.assertEqual(self.opt_base.get_misc_lpar_info(), {'1': {'curr_shared_proc_pool_id': 0, 'mem_mode': '0\n'},
                                                             '2': {'curr_shared_proc_pool_id': 0, 'mem_mode': '0\n'},
                                                             '3': {'curr_shared_proc_pool_id': 0, 'mem_mode': '0\n'},
                                                             '4': {'curr_shared_proc_pool_id': 0, 'mem_mode': '0\n'},
                                                             '5': {'curr_shared_proc_pool_id': 0, 'mem_mode': '0\n'},
                                                             '6': {'curr_shared_proc_pool_id': 0, 'mem_mode': '0\n'}})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_virtual_eth_adapter_id(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['value\n','\n','5'])
        self.assertEqual(self.opt_base.get_virtual_eth_adapter_id(), '5')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_hostname(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['ivm31'])
        self.assertEqual(self.opt_base.get_hostname(), 'ivm31')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_disk_names_for_vhost_san(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['0x8100000000000000:/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2:0x8200000000000000:hdisk11'])
        vhost = 'vhost0'
        self.assertEqual(self.opt_base.get_disk_names_for_vhost(vhost), ['/var/vio/VMLibrary/76e40a6d-c1e0-43c3-9716-629cc556dfd2', 'hdisk11'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_disk_names_for_vhost_local(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['0x8200000000000000:/var/vio/VMLibrary/0270c6c4-d5ba-4004-9e1e-7158fa8fd0fe:0x8100000000000000:lv00'])
        vhost = 'vhost0'
        self.assertEqual(self.opt_base.get_disk_names_for_vhost(vhost,local=True), ['lv00', '/var/vio/VMLibrary/0270c6c4-d5ba-4004-9e1e-7158fa8fd0fe'])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_attach_disk_to_vhost(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['vtscsi5 Available'])
        vhost = 'vhost0'
        disk_info = {'device_name':'hdisk12'}
        self.assertEqual(self.opt_base.attach_disk_to_vhost(disk_info,vhost), None)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_memory_info(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['32768,19584,1408,64'])
        self.assertEqual(self.opt_base.get_memory_info(), {'total_mem': 32768,
                                                           'avail_mem': 19584,
                                                           'sys_firmware_mem': 1408,
                                                           'mem_region_size': 64})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_memory_info_for_lpars(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['06-09FDA,4096\n'
                                               ,'instance-00000011,1024\n'
                                               ,'instance-00000004,2048\n'])
        self.assertEqual(self.opt_base.get_memory_info_for_lpars(), {'06-09FDA': 4096,
                                                           'instance-00000011': 1024,
                                                           'instance-00000004': 2048})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_host_cpu_frequency(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['512000000'])
        self.assertEqual(self.opt_base.get_host_cpu_frequency(), 512L)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_command')
    def test_get_host_cpu_util_data(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['1860293997311027,46524438109395'])
        self.assertEqual(self.opt_base.get_host_cpu_util_data(), {'total_cycles': 1860293997311027L,
                                                                  'used_cycles': 46524438109395L})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_host_uptime(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['03:47PM   up 4 days,   5:42,  12 users,  load average: 0.32, 0.43, 0.36'])
        host = 'ivm31'
        self.assertEqual(self.opt_base.get_host_uptime(host), '03:47PM   up 4 days,   5:42,  12 users,  load average: 0.32, 0.43, 0.36')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_cpu_info(self, mock_run_vios_command):
        mock_run_vios_command.return_value = (['8.00,5.50,64,64'])
        self.assertEqual(self.opt_base.get_cpu_info(), {'total_procs': 8.00,
                                                        'avail_procs': 5.50,
                                                        'max_vcpus_per_aix_linux_lpar': 64,
                                                        'max_procs_per_aix_linux_lpar': 64})

    @mock.patch('nova.openstack.common.processutils.execute')
    def test_run_interactive(self, mock_processutils_execute):
        cmd = ['oem_setup_env', 'lsvg -o', 'exit']
        mock_processutils_execute.return_value = ('datavg\nrootvg\n', '')
        self.assertEqual(self.opt_ivm.run_interactive(cmd), [])

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.IVMOperator.run_interactive')
    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_disk_info(self,mock_run_vios_command, mock_run_interactive):
        mock_run_interactive.return_value = ['rootvg']
        mock_run_vios_command.return_value = ['558 (571392 megabytes):261 (267264 megabytes):297 (304128 megabytes)'
                                                ,'DISK BLOCK SIZE:    512']
        self.assertEqual(self.opt_ivm.get_disk_info(), {'disk_total': 571392,
                                                         'disk_used': 267264,
                                                         'disk_avail': 304128,
                                                         'usable_local_mb': 304128})

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_get_logical_vol_size(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['30:1024 megabyte(s)']
        diskname = 'hd1'
        self.assertEqual(self.opt_ivm.get_logical_vol_size(diskname), 30)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_rename_lpar(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['30:1024 megabyte(s)']
        instance_name = 'instance001'
        new_name = 'instance_test'
        self.assertEqual(self.opt_base.rename_lpar(instance_name, new_name), 'instance_test')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_check_media_library_exists(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['Size(mb) Free(mb) Parent Pool         Parent Size      Parent Free\n'
                                              ,'52012    39240 rootvg                   571392           293888\n'
                                              ,''
                                              ,'Name                                                  File Size Optical         Access\n'
                                              ,'0270c6c4-d5ba-4004-9e1e-7158fa8fd0fe                       3193 vtopt0          ro\n'
                                              ,'76e40a6d-c1e0-43c3-9716-629cc556dfd2                       3193 None          ro\n']
        self.assertEqual(self.opt_base.check_media_library_exists(), True)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_create_fbo_device(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['vtopt5 Available']
        vhost = 'host0'
        self.assertEqual(self.opt_base.create_fbo_device(vhost), 'vtopt5')

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_remove_fbo_device(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['vtopt5 deleted']
        vdev_fbo_name = 'vtopt5'
        self.assertEqual(self.opt_base.remove_fbo_device(vdev_fbo_name), None)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_load_vopt_device(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['']
        vopt_img_name = 'config_20150423150055_6291.iso'
        vdev_fbo_name = 'vtopt5'
        self.assertEqual(self.opt_base.load_vopt_device(vopt_img_name, vdev_fbo_name), None)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_unload_vopt_device(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['']
        vdev_fbo_name = 'vtopt5'
        self.assertEqual(self.opt_base.unload_vopt_device( vdev_fbo_name), None)

    @mock.patch('paxes_nova.virt.ibmpowervm.ivm.operator.BaseOperator.run_vios_command')
    def test_check_lpar_started(self,mock_run_vios_command):
        mock_run_vios_command.return_value = ['Linux ppc64']
        lpar_id = '3'
        self.assertEqual(self.opt_base.check_lpar_started( lpar_id), True)
