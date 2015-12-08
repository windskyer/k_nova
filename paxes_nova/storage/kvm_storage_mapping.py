# vim: tabstop=4 shiftwidth=4 softtabstop=4
# =================================================================
# =================================================================

import ConfigParser
from nova import db
from nova.openstack.common import log as logging
from powervc_nova import _, logcall
from powervc_nova.storage import storage_utils as su


# See kvm_registrar.KVMHostRegistrar.___set_nfs_config_values() for the
# location where these configuration properties are set.
CONF_SERVER = 'kvm_host_network_server'
CONF_SHARE = 'kvm_host_network_share'
CONF_HOST_PATH = 'kvm_host_ephemeral_path'
CONF_STORAGE_TYPE = 'host_storage_type'

LOG = logging.getLogger(__name__)


class KVMStorageMapping():
    """
    Internal API module for storage mapping functions in an KVM Linux
    host environment.
    """
    @logcall
    def get_viable_hosts(self, context, instance_uuid=None, instance=None,
                         flavor=None, vol_type=None, image=None,
                         msg_dict=None):
        """
        Given a VS instance, lookup the storage connectivity
        information and then return viable (candidate) hosts that
        match the storage requirements implied by the instance. The
        method signature here matches that of the corresponding method
        for HMC environments so that the same API calling code can be
        used without changes; however, some parameters are not supported
        for KVM.

        :param context: A context object that is used to authorize
                       the DB access.
        :param instance_uuid: The instance UUID that will be used to
                       get the storage requirements of an instance. From
                       the instance -- the flavor, image, and volume_type
                       will be looked up (if they are available), and those
                       items will be used in filtering the viable hosts
                       as if they were passed in as options to this API.
        :param instance: Optional instance object if it is available.
                       If passed, that object will be used directly rather
                       than looking it up through the instance_uuid.
        :param flavor: Optional flavor object that can be used to get the
                       volume type if not provided and/or instance does not
                       have it.
                       **This option is ignored in 1.2.1 since NFS storage
                         is not managed (no vol types)
        :param vol_type: Optional volume type dictionary for use in filtering
                       by the target storage provider. If the provider that
                       the vol_type applies to is not available, then no
                       hosts will be returned.
                       **This option is ignored in 1.2.1 since NFS storage
                         is not managed (no vol types)
        :param image:  Optional image. If both the image and vol_type is
                       provided, then a check for enough space to deploy the
                       image on the target storage will be made. If it fails,
                       no hosts will be returned.
                       **This option is ignored in 1.2.1 since NFS storage
                         is not managed.
        :param msg_dict: Optional dictionary containing a key of 'messages'
                       with a list for its value. Informational and warning
                       messages will be appended to this list for use in
                       REST return structures.
        :returns: A dictionary of child host dictionaries keyed by
                  host (registered name).
        """
        # If we don't have the instance yet, look it up
        if instance_uuid and not instance:
            instance = db.instance_get_by_uuid(context, instance_uuid)
            LOG.debug("Instance***: %s." % instance.__dict__)
        inst_d = instance.__dict__ if instance is not None else "n/a"
        # Log the instance, other params handled by method decorator
        LOG.debug("Incoming instance=%s" % inst_d)

        db_hosts = su.get_compute_nodes_from_DB(context)
        if not db_hosts and msg_dict:
            msg_dict['messages'].append(_("No hosts are managed."))

        # Check to see if the instance already is hosted (e.g. migrate op)
        if not instance or not instance.get('host'):
            # In 1.2.1, there is no flavor option or volume type that
            # would support a request to only one type of boot storage.
            msg = _("Since the request is in regard to a deploy operation "
                    "or the instance was not provided for filtering, all "
                    "KVM hosts will be considered viable. To deploy to hosts "
                    "with shared storage only, a targeted deploy to a "
                    "specific host must be requested.")
            LOG.info(msg)
            if msg_dict:
                msg_dict['messages'].append(msg)
            db_hosts = su.get_compute_nodes_from_DB(context)
            return dict([[x, {'host': x}] for x in db_hosts])

        # The instance is currently hosted - either onboarded or deployed.
        # We want to return hosts that could host the instance from
        # a storage standpoint. First we get the storage CONF information
        # from the source host.
        # Note: We don't have to get the disk information for the instance
        #       in 1.2.1 because we can assume that there is only the
        #       single boot disk and it is using storage from a single
        #       host location - described by the storage config props here.
        # Note: We don't filter out the current host.
        source_props = self._get_storage_conf_props(instance['host'])

        if source_props[CONF_STORAGE_TYPE] != 'nfs':
            msg = _("The source host, %(host)s, for the instance is not "
                    "providing supported shared storage, so instance "
                    "'%(instance)s' cannot be migrated to another host. The "
                    "host storage type is '%(type)s'.") %\
                dict(host=instance['host'], instance=instance['name'],
                     type=source_props[CONF_STORAGE_TYPE])
            LOG.info(msg)
            if msg_dict:
                msg_dict['messages'].append(msg)
            return {}

        # Loop over hosts, matching those with like storage.
        ret_dict = {}
        log_msgs = []
        conf_files = []
        for db_host in db_hosts:
            prefix = _("Host '%s' is filtered out because it does "
                       "not ") % db_host
            host_props = self._get_storage_conf_props(db_host)
            if host_props[CONF_STORAGE_TYPE] != 'nfs':
                log_msgs.append(prefix + _("use NFS for ephemeral storage."))
                conf_files.append(host_props['conf_file'])
            elif host_props[CONF_SERVER] != source_props[CONF_SERVER] or\
                    host_props[CONF_SHARE] != source_props[CONF_SHARE]:
                log_msgs.append(prefix + _("specify the same network server "
                                           "and share as the source host."))
                conf_files.append(host_props['conf_file'])
            elif host_props[CONF_HOST_PATH] != source_props[CONF_HOST_PATH]:
                log_msgs.append(prefix + _("specify the same host mount path "
                                           "as the source host."))
                conf_files.append(host_props['conf_file'])
            else:
                ret_dict[db_host] = {'host': db_host}

        # TODO: It would be nice to check for sufficient space on the
        #       storage provider to contain the image or the VM disk
        #       like we do for the HMC version of this API. But current
        #       NFS space is not a host metric being flowed back to the
        #       management server, and in a follow-on release, the NFS
        #       provider (or GPFS provider) will be managed and the free
        #       space metrics would come from the provider rather than the
        #       host, so this is left as a future exercise.
        LOG.debug("Returning viable hosts for instance '%s': %s" %
                  (instance['name'], ret_dict))
        if log_msgs:
            LOG.info(_("Hosts excluded: %(msg)s. Check the following "
                       "management server configuration files for details: "
                       "targets=%(target)s, source=%(source)s") %
                     dict(msg=log_msgs, target=conf_files,
                          source=source_props['conf_file']))
        return ret_dict

    def _get_storage_conf_props(self, host_name):
        """ Get storage configuration properties from the management
            server's 'shadow' nova-<host>.conf file.
            These may show up in the DB entry for the host. If they do,
            we could switch to grab them from there or maintain this
            mapping in-memory.
        """
        filename = '/etc/nova/nova-' + host_name + '.conf'
        keys = [CONF_SERVER, CONF_SHARE, CONF_HOST_PATH, CONF_STORAGE_TYPE]
        config = {'conf_file': filename}
        parser = ConfigParser.RawConfigParser()
        parser.read(filename)
        for key in keys:
            if parser.has_option('DEFAULT', key):
                config[key] = parser.get('DEFAULT', key)
            else:
                config[key] = None
        LOG.debug("Host %s has storage config props: %s" % (host_name, config))
        return config
