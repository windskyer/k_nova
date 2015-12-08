# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from oslo.config import cfg

ibmpowervm_opts = [
    cfg.StrOpt('powervm_vif_driver',
               default='paxes_nova.'
               'virt.ibmpowervm.vif.ivm.local_driver_ivm.IBMPowerVMVlanVIFDriverLocal',
               help='Driver to plug/Unplug VLANs into adapters'),
    cfg.StrOpt('powervm_vif_topo_driver',
               default='paxes_nova.'
               'virt.ibmpowervm.vif.ivm.local_topo_ivm.IBMPowerVMNetworkTopoIVMLocal',
               help='Driver to gather the topology of the network.'),
    cfg.StrOpt('powervm_base_operator_factory',
               default='paxes_nova.virt.ibmpowervm.ivm.local_operator.BaseOperatorFactory',
               help='Run VIOS command with SSH or local')
]

CONF = cfg.CONF
CONF.register_opts(ibmpowervm_opts)
