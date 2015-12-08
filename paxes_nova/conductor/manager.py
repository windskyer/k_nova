#
#
# =================================================================
# =================================================================

"""Overrides the Conductor Manager for additional PowerVC DB Access:
    1. Query HMC Information for a given Host
    2. Add/Update/Deleted/Query VIOS Information
    3. Add/Update/Delete/Query Adapter Information (SEA/VEA/HBA)
    4. Add/Update/Delete/Query Host Metric/Status Information
    5. Add/Update/Delete/Query LPAR Allocation Information
"""

import sys
import copy
import nova.context
from oslo import messaging
from nova import rpc as rpc_api
from nova.conductor import manager
from nova.conductor.tasks import live_migrate
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.objects import service as service_obj
from paxes_nova.compute import api as compute_api
from paxes_nova.db import api as db_api
from paxes_nova.objects.compute import dom as compute_dom
from paxes_nova.objects.network import dom as network_dom
from paxes_nova.objects.storage import dom as storage_dom
from paxes_nova import _

LOG = logging.getLogger(__name__)
HOST_REG = 'powervc_discovery.registration.compute.host_registrar'


class PowerVCConductorManager(manager.ConductorManager):
    """Extends the base Conductor Manager class with PowerVC capabilities"""

    def __init__(self, *args, **kwargs):
        """Constructor for the PowerVC extension to the Conductor Manager"""
        super(PowerVCConductorManager, self).__init__(*args, **kwargs)
        self.additional_endpoints.append(_PowerVCConductorManagerProxy(self))
        # Construct a Default Factory from each Module to allow initialization
        self.hmcfac = compute_dom.ManagementConsoleFactory.get_factory()
        self.seafac = network_dom.SharedEthernetAdapterFactory.get_factory()
        self.scgfac = storage_dom.StorageConnectivityGroupFactory.get_factory()
        # We need to create the default SCG the first time the Conductor starts
        self.scgfac.create_default_fc_scg(nova.context.get_admin_context())

    #######################################################
    ##########  Override Initialization Methods  ##########
    #######################################################
    def pre_start_hook(self, **kwargs):
        """Override the Pre-Start Hook to perform some initialization"""
        # Call the Parent preStart hook if it ever implements anything
        super(PowerVCConductorManager, self).pre_start_hook(**kwargs)
        try:
            # Force the DB API to initialize by doing a random query for HMC's
            self.hmcfac.find_all_hmcs(nova.context.get_admin_context())
        except:
            pass  # We don't care if it fails, since it is just initialization

    ###################################################
    ###### PowerSpec Conductor API Implementation #####
    ###################################################
    def instance_update(self, context, instance_uuid,
                        updates, service=None):
        """ Update the attributes for the Instance given in the Database """
        updates = copy.deepcopy(updates)
        pwr_specs = updates.pop('power_specs', None)
        #Call the Parent's method to actually update the Instance in the DB
        instance_ref = super(PowerVCConductorManager, self).\
            instance_update(context, instance_uuid, updates, service)
        #If there were any Power Specs given, update those in the database
        if pwr_specs is not None:
            pwr_specs = db_api.\
                instance_power_specs_update(context, instance_uuid, pwr_specs)
            instance_ref['power_specs'] = self._convert_power_specs(pwr_specs)
        return instance_ref

    ####################################################
    #########  Network Adapter Implementation  #########
    ####################################################
    def host_reconcile_network(self, context, host_networking_dict):
        """ Sends the collected topology of a host's networking resources """
        LOG.info('pvc_nova.conductor.manager.PowerVCConductorManager '
                 'host_reconcile_network: context, '
                 'host_networking_dict len()= '
                 + str(len(host_networking_dict)))
        db_api.host_reconcile_network(context, host_networking_dict)

    ####################################################
    ##########  UnManage Host Implementation  ##########
    ####################################################
    def notify_unmanage(self, context, host, mgmt_ip):
        """Notifies this Management System to remove Host Management"""
        info = dict(hostname=host, ip=mgmt_ip)
        try:
            #Log a message for debug purposes that were are removing the Host
            LOG.info(_("Removing Host %(hostname)s, switching "
                       "to Management System %(ip)s...") % info)
            #Generate a Context with a Token to use for the Requests
            context = self._generate_admin_context()
            #If the Compute Node doesn't exist, we don't need to notify
            comp_node = self._get_compute_node(context, host)
            if comp_node is None:
                return
            #Notify the old Management System is no longer managing the host
            text = _("The PowerVC management system at %(ip)s is taking over "
                     "management of host %(hostname)s.  The host will be "
                     "removed from this management system.") % info
            anotifier = rpc_api.get_notifier('compute', host)
            anotifier.info(context, 'compute.instance.log', {'msg': text})
            try:
                __import__(HOST_REG)
                #Call the Host Registrar to do the full clean-up of the Host
                get_registrar = getattr(sys.modules[HOST_REG], 'get_registrar')
                registrar = get_registrar(context, host_name=host)
                registrar.skip_remote_commands = True
                registrar.deregister(force=True)
            except Exception as ex:
                LOG.warn(_("Exception trying to fully remove the Host."))
                LOG.exception(ex)
                #Send a notification that we are removing the Compute Node
                anotifier.info(context, 'compute.node.delete.start', comp_node)
                #Fall-back to just cleaning the DB, if the main flow failed
                hostfact = compute_dom.ManagedHostFactory.get_factory()
                hostfact.delete_host(context, host)
                #Send a notification that we removed the Compute Node
                anotifier.info(context, 'compute.node.delete.end', comp_node)
            #Log a message for debug purposes that we removed the Host
            LOG.info(_("Removed Host %(hostname)s, switching "
                       "to Management System %(ip)s.") % info)
        except Exception as exc:
            #Log the Exception that occurred while trying to Remove the Host
            LOG.warn(_("Failed to remove Host %(hostname)s while "
                       "switching to Management System %(ip)s") % info)
            LOG.exception(exc)

    ####################################################
    ########  Internal Conductor Helper Methods  #######
    ####################################################
    @staticmethod
    def _get_compute_node(context, host):
        """Helper method to query the Compute Node from the DB"""
        service = service_obj.Service.get_by_compute_host(context, host)
        #If we weren't able to find the Server or Compute Node, just return
        if service is None or service['compute_node'] is None:
            return None
        #Return the key info from the Compute Node as a dictionary
        compute_node = service['compute_node']
        return dict(compute_node_id=compute_node.id,
                    host=service.host, service_id=compute_node.service_id,
                    hypervisor_hostname=compute_node.hypervisor_hostname)

    @staticmethod
    def _generate_admin_context():
        """Helper method to create a Context with a Token/ServiceCatalog"""
        from nova.network import neutronv2
        __import__('nova.network.neutronv2.api')
        context = nova.context.get_admin_context()
        #We are using the Neutron Client since they having Caching logic
        nclient = neutronv2.get_client(context).httpclient
        #Since the Neutron Client is cached, the token may already be
        #populated, so only need to authenticate if it isn't set yet
        if nclient.auth_token is None:
            nclient.authenticate()
        context.auth_token = nclient.auth_token
        context.service_catalog = \
            nclient.service_catalog.catalog['access']['serviceCatalog']
        return context

    @staticmethod
    def _convert_power_specs(power_specs):
        """Internal Helper Method to Convert PowerSpecs to a Dictionary"""
        lst = ['created_at', 'updated_at', 'deleted_at', 'deleted', 'instance']
        power_specs = jsonutils.to_primitive(power_specs)
        #There are certain properties on the PowerSpecs we don't want returned
        for attr in lst:
            if attr in power_specs:
                power_specs.pop(attr, None)
        return power_specs


###################################################
#### Conductor Manager Adapter Extension Class ####
###################################################
class _PowerVCConductorManagerProxy(object):
    """Adapter Class to extend Remote Methods for the Conductor"""

    target = messaging.Target(version='2.0')

    def __init__(self, manager):
        """Construct a Class to extend Remote Methods for the Conductor"""
        self.manager = manager

    def notify_unmanage(self, context, host, mgmt_ip):
        """Notifies this Management System to remove Host Management"""
        return self.manager.notify_unmanage(context, host, mgmt_ip)


###################################################
#########   Override Live Migration Task   ########
###################################################
#Since the default RPC timeout in OpenStack is way too low in many cases for
#migrations, Paxes is extending a few of the RPC API call methods to set a
#larger timeout (based on a configuration property). This works in most places
#in the Compute Manager since we specify the Paxes RPC API there, but for
#the check_can_live_migrate_destination call in the live_migration module this
#directly constructs its own RPC API, so it doesn't pick up the override.
#
#We need to have a better solution for 2Q14 (larger default, OSCE increasing
#timeout, etc), but temporarily for 1.2 FP1 are putting in a change to inject
#our own extended LiveMigrationTask class that only overrides the RPC API
#used. This will inject our own execute method that just constructs this Task.
class _Extended_LiveMigrationTask(live_migrate.LiveMigrationTask):
    """Extends the default Live Migration Task to override the RPC API used"""

    def __init__(self, context, instance, destination,
                 block_migration, disk_over_commit):
        """Constructor for the PowerVC extension to Live Migration Task"""
        super(_Extended_LiveMigrationTask, self).\
            __init__(context, instance, destination,
                     block_migration, disk_over_commit)
        #Use our own extended RPC API instead of the default
        self.compute_rpcapi = compute_api.PowerVCComputeRPCAPI()


def _extended_live_migrate_execute(context, instance, destination,
                                   block_migration, disk_over_commit):
    """Overrides the default live_migrate execute to use our extended Task"""
    task = _Extended_LiveMigrationTask(context, instance, destination,
                                       block_migration, disk_over_commit)
    return task.execute()


#Inject our own overwritten live migrate execute function instead
setattr(live_migrate, 'execute', _extended_live_migrate_execute)
