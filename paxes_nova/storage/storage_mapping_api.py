# vim: tabstop=4 shiftwidth=4 softtabstop=4
# =================================================================
# =================================================================

from nova import db
from nova.compute import flavors
from nova.image import glance
from nova import exception
from nova.openstack.common import jsonutils, log as logging
from powervc_nova import _
from powervc_nova import logcall
from powervc_nova.conductor import api as conductor
from powervc_nova.db import api as pvcdb
from powervc_nova.storage import exception as stgex, storage_utils as su
from powervc_nova.virt.ibmpowervm.common.constants \
    import CONNECTION_TYPE_NPIV, CONNECTION_TYPE_SSP
from powervc_nova.volume import cinder as pvc_cinder
from cinderclient.exceptions import NotFound
import nova.db.sqlalchemy.api as db_api


LOG = logging.getLogger(__name__)


class StorageMapping():

    """
    Internal API module for storage mapping functions using storage
    connectivity groups. For use by the nova scheduler and GUI in determining
    candidate hosts and by deploy processing to get candidate VIOS and
    FC Port information.
    This class is wired in the same way as
    powervc_nova/network/neutronv2/ibmpowervm_api.py
    So it should be imported and methods called directly as per caller(s)
    of PowerVMAPI.get_viable_hosts(). As such, it is cross-host and is not
    architected to be called through RPC.
    """

    def __init__(self):
        # No base class at this time
        self.image_service = glance.get_default_image_service()

    def get_viable_hosts(self, context, instance_uuid=None, instance=None,
                         flavor=None, vol_type=None, image=None,
                         msg_dict=None):
        """
        Given a VS instance or a flavorRef, lookup the storage connectivity
        group (SCG) and then return viable (candidate) hosts that
        match the storage requirements defined by the SCG associated either
        with the instance or flavor provided.

        :param context: A context object that is used to authorize
                       the DB access.
        :param instance_uuid: The instance UUID that will be used to
                       get the storage requirements of an instance. From
                       the instance -- the flavor, SCG, image, and volume_type
                       will be looked up (if they are available), and those
                       items will be used in filtering the viable hosts
                       as if they were passed in as options to this API.
        :param instance: Optional instance object if it is available.
                       If passed, that object will be used directly rather
                       than looking it up through the instance_uuid.
        :param flavor: Optional flavor object that can be used to get the
                       SCG to do the filtering when an instance is not
                       provided. Required when an image is given.
        :param vol_type: Optional volume type dictionary for use in filtering
                       by the target storage provider. If the provider that
                       the vol_type applies to is not available, then no
                       hosts will be returned.
        :param image:  Optional image. If both the image and vol_type is
                       provided, then a check for enough space to deploy the
                       image on the target storage will be made. If it fails,
                       no hosts will be returned.
        :param msg_dict: Optional dictionary containing a key of 'messages'
                       with a list for its value. Informational and warning
                       messages will be appended to this list for use in
                       REST return structures.
        :returns: A dictionary of child host dictionaries keyed by
                  host (registered name).
        """
        # The definition of a SCG directly maps to a list of VIOS
        # resources and their hosts, so the basic implementation is
        # straightforward, but additional filtering is needed to exclude
        # hosts where its VIOSes have no real time connectivity --
        #   (a) get_connectivity_info() fails to find connectivity options.
        #   (b) The storage provider being targeted is not usable for some
        #       reason.
        inst_d = instance.__dict__ if instance is not None else "n/a"
        LOG.debug("**ENTRY**  instance_uuid=%s, instance=%s, "
                  "flavor=%s, image=%s, vol_type=%s." %
                  (instance_uuid, inst_d, flavor, image, vol_type))

        # If we don't have the instance yet, look it up
        if instance_uuid and not instance:
            instance = db.instance_get_by_uuid(context, instance_uuid)
            LOG.debug("Instance***: %s." % instance.__dict__)

        #############################################################
        # Do storage backend checks. If storage not OK or not       #
        # enough space, then no hosts will be applicable,           #
        # regardless of the SCG.                                    #
        #############################################################
        try:
            if not self._is_target_storage_usable(context, instance, flavor,
                                                  image, vol_type, msg_dict):
                return {}  # empty dict
            LOG.debug("The target storage is determined to be usable.")
        except Exception as ex:
            # Just log this condition and continue
            msg = _("Could not check backend storage due to an unexpected "
                    "problem (See nova logs for details). Continuing with "
                    "host-side viability checks.")
            LOG.warn(msg)
            if msg_dict:
                msg_dict['messages'].append(msg)
            LOG.exception(ex)

        if not flavor and not instance and not image:
            msg = _("Since neither a flavor or an instance or an image was "
                    "provided to filter hosts based on a determined storage "
                    "connectivity group, all hosts will be considered viable.")
            LOG.info(msg)
            if msg_dict:
                msg_dict['messages'].append(msg)
            db_hosts = su.get_compute_nodes_from_DB(context)
            return dict([[x, {'host': x}] for x in db_hosts])

        #############################################################
        # Use common method entry to determine the SCG.             #
        # Cases:                                                    #
        #   pre-deploy --> "storage-viable-hosts"(API) -->          #
        #        get_viable_hosts(flavor, image)                    #
        #   deploy --> scheduler --> get_viable_hosts(instance) --> #
        #        get_scg_from_flavor(flavor)                        #
        #############################################################
        LOG.debug("Calling determine_scg() for pre-deploy or deploy "
                  "scheduler case.")
        response = self.determine_scg(context, instance=instance,
                                      flavor=flavor, image=image,
                                      raise_err=True)
        scg = response['scg']
        if msg_dict:
            msg_dict['messages'].extend(response['messages'])

        LOG.debug("Determined SCG='%s'. Now check connectivity for member "
                  "hosts: %s." % (scg.display_name, scg.get_host_name_list()))
        #################################################################
        # Using the host members of the determined SCG, get a filtered  #
        # list of only the ones that have at least one VIOS with        #
        # connectivity (proper FC Ports, enabled, etc.)                 #
        #################################################################
        verified_host_list = scg.verify_host_connectivity(context,
                                                          msg_dict=msg_dict)

        # Now transform the host list to the nested dictionary structure
        # expected by consumers of the Paxes-only API.
        return dict([[x, {'host': x}] for x in verified_host_list])

    def _is_target_storage_usable(self, context, instance, flavor, image,
                                  vol_type, msg_dict):
        """
        The intent of this helper method is to provide an evaluation of
        whether the target storage for a deploy is available and has
        sufficient space to accommodate the image. So its utility is likely
        limited to the get_viable_hosts() API. If instance is not None,
        then this is the pre-deployed instance (and the flavor-->boot-volume
        type as well as image will be looked up from it) or an instance
        to be migrated. Otherwise if instance is None, then a management UI
        is likely calling to determine viability prior to instance creation.
        The flavor, image, and vol_type params are used for equivalent
        storage determinations in that case.

        Currently, the approach is to error on the side of
        usability so that deploys are not prevented unless they clearly
        should be. This method returns False only if all information
        sought is available and one of the following situations is hit:
          (1) The target provider is in 'error' state
          (2) The target provider has a free capacity that is less than the
              image size.
          (3) The provider of the image's backing volume does not match the
              provider of the target volume_type. This is a current PowerVC
              limitation that we can optimize for by checking here. If the
              limitation is relaxed, this check needs to be removed.
          (4) The SCG in the flavor does not provide access to the
              provider of the target volume_type. This check catches #1 as
              well since providers in error state are not accessible, but
              it is helpful to have a separate error message for #1.
        """
        def log_and_return_false(msg):
            LOG.warn(msg)
            if msg_dict:
                msg_dict['messages'].append(msg)
            return False

        image_size = 0.0  # init image size in GBs
        cinder = None
        image_vol_provider = None
        scg_id = None
        err_str = _("No hosts are viable for connectivity.")

        if instance:
            meta = None
            uuid = instance['uuid']
            if not flavor and not vol_type:
                meta = db_api.instance_system_metadata_get(context, uuid)
                flavor = db.flavor_get_by_flavor_id(
                    context, meta['instance_type_flavorid'])
            if not image:
                if not meta:
                    meta = db_api.instance_system_metadata_get(context, uuid)
                if 'image_min_disk' in meta:
                    image_size = float(meta['image_min_disk'])
                else:
                    LOG.debug('image_min_disk not part of instance system '
                              'metadata.')
                if 'image_volume_mapping' in meta:
                    image_vol_map = meta['image_volume_mapping']
                    if image_vol_map:
                        vol_map = jsonutils.loads(image_vol_map)
                        if vol_map:
                            # Check first volume in image only.
                            source_volid = vol_map[0]['source_volid']
                            LOG.debug("source_volid=%s" % source_volid)
                            if not cinder:
                                cinder = pvc_cinder.PowerVCVolumeAPI()
                            try:
                                vol = cinder.get(context, source_volid)
                                image_vol_provider = vol['host']
                            except Exception as ex:
                                LOG.debug('Image volume not found: %s'
                                          % source_volid)
            LOG.debug('Instance uuid=%(uuid)s, system metadata=%(meta)s, '
                      'flavor=%(flavor)s, image_vol_provider=%('
                      'image_vol_provider)s.' % locals())
        if image:
            image_size = float(image['size']) / (1024 ** 3)

        if flavor and flavor['extra_specs']:
            specs = flavor['extra_specs']
            scg_id = specs.get('powervm:storage_connectivity_group')
            LOG.debug("Flavor SCG ref=%s." % scg_id)
            if not vol_type:
                vtype_id = specs.get('powervm:boot_volume_type')
                LOG.debug("Flavor volume type ref=%s." % vtype_id)
                if vtype_id:
                    cinder = pvc_cinder.PowerVCVolumeAPI()
                    vol_type = cinder.get_volume_type(context, vtype_id)

        # Now we've acquired data if possible. Do the checks
        if vol_type:
            p_id = vol_type['extra_specs']['capabilities:volume_backend_name']
            if not cinder:
                cinder = pvc_cinder.PowerVCVolumeAPI()
            provider = cinder.get_stg_provider_info(context,
                                                    storage_hostname=p_id,
                                                    use_cache=True)
            LOG.debug('Volume_type=%(vol_type)s, provider=%(provider)s, '
                      'Image size_GiB = %(image_size)f.' % locals())
            provider_name = provider['host_display_name']
            if provider['backend_state'] == 'error':
                msg = _("The target storage provider '%(name)s' is not usable "
                        "since it is in 'error' state. Correct the storage "
                        "problem and try the request again. %(err)s")
                msg = msg % dict(name=provider_name, err=err_str)
                return log_and_return_false(msg)

            provider_id = provider['storage_hostname']
            if image_vol_provider and image_vol_provider != provider_id:
                msg = _("The storage provider ID '%(image_prov_id)s' of the "
                        "image volume does not match the storage provider ID "
                        "'%(prov_id)s' specified in the target volume type "
                        "'%(type)s'. Provide a different image or volume type "
                        "and try the request again. %(err)s")
                msg = msg % dict(image_prov_id=image_vol_provider,
                                 prov_id=provider_id, type=vol_type['name'],
                                 err=err_str)
                return log_and_return_false(msg)

            if scg_id:
                scg = su.get_scg_by_id(context, scg_id)
                if provider_id not in scg.to_dict()['applicable_providers']:
                    msg = _("The storage connectivity group '%(scg)s' does "
                            "not currently provide access to the storage "
                            "provider '%(prov)s' specified in the target "
                            "volume type '%(type)s'. Specify a different "
                            "storage connectivity group or volume type and "
                            "try the request again. %(err)s")
                    msg = msg % dict(scg=scg.display_name, prov=provider_name,
                                     type=vol_type['name'], err=err_str)
                    return log_and_return_false(msg)

            # 'free_capacity_gb' should also be a float
            if image_size and provider['free_capacity_gb'] < image_size:
                msg = _("Target storage provider '%(prov)s' is not usable "
                        "since it reports a free capacity of "
                        "'%(free_size).1f' GBs, but the image size is "
                        "'%(image_size).1f' GBs. Free up storage and try the "
                        "request again. %(err)s")
                msg = msg % dict(prov=provider_name,
                                 free_size=provider['free_capacity_gb'],
                                 image_size=image_size, err=err_str)
                return log_and_return_false(msg)

        # Default to assuming storage is usable.
        return True

    def get_connectivity_info(self, context, instance_uuid,
                              host_name, instance=None, extra_checking=False,
                              live_data=True):
        """
        Given a VS instance and a specific host chosen by the scheduler,
        lookup the storage connectivity group (SCG) for the instance
        and determine applicable connectivity options for the instance.
        If the SCG specifies Storage Controller connectivity,
        then FC Port information is included. If for SSP only, then no
        'ports' element will be included per VIOS. If present,
        'ports' will be categorized by 'fabric' and will be filtered on
        their 'enabled' status and 'port_tag', if any specified by the FC
        Port and SCG.

        :param context: A context object that is used to authorize
                        the DB access.
        :param instance_uuid: The VS instance id that is being deployed.
                        We use that to look up the storage connectivity group
        :param instance: Optional instance object if it is available.
                If passed, that object will be used directly rather
                than looking it up through the instance_uuid.
        :param host_name: The host to constrain the VIOS candidates to.
                          This is the registered compute host name.
        :param extra_checking: If this flag is passed as True, then extra
                               checks are done against the storage providers
                               and instance image data.
        :param live_data: Go to HMC K2 API to retrieve host topology data
                          if this parm is True, else only use DB. Live
                          data allows best-fit ordering of FC Ports when
                          multiple ports are available per VIOS and fabric,
                          but is slower.
        :returns: A dictionary of connectivity information.
        NPIV (FC controller) example where SCG also has SSP connectivity:
         {
         "connection-types": [
          "npiv", "ibm_ssp"
         ],
         "vios_list": [
          {
           "lpar_id": 1,
           "name": "hv4lpar05a",
           "id": "8203E4A_10007CA##1",
           "state": "running",
           "rmc_state": "active",
           "connection-types": ["npiv", "ibm_ssp"],
           "ports": {
            "A": [
             {
              "udid": "1aU789C.001.AAA0140-P1-C1-T2",
              "wwpn": "10000000C9BB327B",
              "name": "fcs1",
              "total_vports": 64,
              "available_vports": 61
             }
            ],
            "B": [
             {
              "udid": "1aU789C.001.AAA0140-P1-C1-T4",
              "wwpn": "10000000C9BB327D",
              "name": "fcs3",
              "total_vports": 64,
              "available_vports": 60
             }
            ]
           }
          }
         ]
        }

        Notes:
            * Instead of "A" or "B" (fabric), you could also see a
              "None" fabric category where ports have not been assigned to a
              dual fabric configuration.
            * A VIOS entry may be applicable to one or more connection types.
              The 'connection-types' entry for the vios will list the types
              that it is applicable for. The top level 'connection-types'
              entry in the returned dictionary is a list of all the possible
              connection types.
            * If a VIOS has ibm_ssp connectivity only, then the 'ports'
              structure will not be returned for it since the SSP sits in
              front of any port requirements.
            * The 'total_vports' property will not
              be present on a port entry if it is not set on the HMC
              side or if the ports were not retrieved "live" in the flow.
        """
        if not instance:
            # Retrieve instance if not provided
            instance = db.instance_get_by_uuid(context, instance_uuid)

        ###############################################################
        # Use common method entry to determine the SCG.               #
        # Cases:                                                      #
        #   deploy --> get_connectivity_info()                        #
        #   relocate --> compute manager --> get_connectivity_info()  #
        ###############################################################
        LOG.debug("Calling determine_scg() for the deploy or relocate "
                  "get_connectivity_info() flow.")
        response = self.determine_scg(context, instance=instance,
                                      host=host_name, raise_err=True)
        scg = response['scg']

        # Get and return the connectivity structure
        cdata = scg.get_connectivity(context, host_name, live_data)
        return cdata

    def get_scg_from_flavor(self, context, flavor, instance_uuid=None,
                            msg_dict=None):
        """
        Get the Storage Connectivity Group associated with the flavor.
        :param context: A context object that is used to authorize
                    any DB access.
        :param flavor: The flavor (or instance_type) of the instance already
                    deployed or the one that will be deployed.
        :param instance_uuid: If the flavor is for an instance that is
                     already deployed (exists), then pass the uuid of the
                     instance for logging purposes.
        :param msg_dict: A dictionary containing a key 'messages' with a list
                     value that informational messages can be appended to.
        :returns: The SCG object that is looked up from the flavor
                    extra_specs.
        """
        scg_key = "powervm:storage_connectivity_group"
        LOG.debug("Flavor=%s" % str(flavor))
        flavor_id = flavor["flavorid"]
        extra_specs = flavor["extra_specs"]
        if scg_key in extra_specs.keys():
            scg_id = extra_specs[scg_key]
            LOG.debug("**RETURN** scg_id=%s" % scg_id)
            try:
                scg = su.get_scg_by_id(context, scg_id)
                return scg
            except Exception as ex:
                LOG.exception(ex)
                msg = _('%s') % ex
                msg = msg + _(" The flavor ID is '%(flavor)s' and the "
                              "instance ID, if any, is '%(instance)s'.") %\
                    dict(flavor=flavor_id, instance=instance_uuid)
        else:
            # The not-found case is no longer considered an error by
            # this mapping utility. The caller can try to look up or
            # determine the SCG in another way.
            msg = stgex.IBMPowerVCSCGNotInFlavor.msg_fmt % locals()
        LOG.info(msg)
        if msg_dict:
            msg_dict['messages'].append(msg)
        LOG.debug("**RETURN** msg_dict=%s" % msg_dict)
        return None

    def determine_scg(self, context, instance=None, flavor=None,
                      image=None, host=None, raise_err=False):
        """
        General purpose method to choose an SCG applicable to a particular
        resource(s). This is not just a 'getter' method. It will look in
        one or more locations for an associated SCG, and if an SCG is
        not found, it will try to pick an SCG based on the resources passed.
        If an applicable one can be picked, and an instance is passed,
        then the picked SCG will be set in the instance's system_metadata so
        the next time this method is called in the same fashion, the result
        will be consistent.
        :returns: A dictionary of the following format:
            {
                'scg': <db-scg-resource>,
                'messages': "<info message>"
            }
            The value for 'scg' will be the DB SCG resource if one could
            be determined. If not, it will be None and if there is an
            explanation 'message' for that case that is available, then the
            the 'message' string will be set with that.
        """
        ret_dict = {'scg': None, 'messages': []}
        if not (instance or flavor or image):
            messages = [_("The storage connectivity group cannot be "
                          "determined because a reference resource (instance,"
                          " flavor, image) was not given.")]
            msg = stgex.IBMPowerVCSCGNotDetermined.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCSCGNotDetermined(msg)

        LOG.debug("**determine_scg** instance=%s, flavor=%s, image=%s, "
                  "host=%s, raise_err=%s"
                  % (None if not instance else instance['uuid'],
                     None if not flavor else flavor['flavorid'],
                     None if not image else image['id'], host, raise_err))

        # If an instance is provided, check that first
        if instance:
            scg = self.get_scg_from_instance_meta(context, instance, ret_dict)
            if scg is not None:
                # Nothing else to do as the SCG is already set
                ret_dict['scg'] = scg
                LOG.debug("Looked up SCG in system_metadata. '%s'(instance) "
                          "--> '%s'(scg)." % (instance['uuid'], scg.id))
            elif not flavor:
                # Else branch added as performance improvement to not
                # require caller to do this lookup if SCG is in metadata.
                type_id = instance["instance_type_id"]
                flavor = flavors.get_flavor(type_id, ctxt=context,
                                            inactive=True)
                LOG.debug("instance_type_id=%s, looked up flavor is %s."
                          % (str(type_id), str(flavor)))

        # If a flavor is available, that is checked next.
        if flavor and ret_dict['scg'] is None:
            scg = self.get_scg_from_flavor(context, flavor,
                                           instance['uuid'] if instance
                                           is not None else None,
                                           msg_dict=ret_dict)
            if scg is not None:
                self.set_scg_to_instance(context, instance, scg, ret_dict)
                ret_dict['scg'] = scg

        if ret_dict['scg'] is None and image is None and instance is not None:
            # Try looking up image for this case from the instance
            image = self.get_image_from_instance(context, instance)
            if image is None:
                msg = _("Unable to look up image for instance uuid '%s'.")
                ret_dict['messages'].append(msg % instance['uuid'])
        # If an image is provided, check that next
        if image and ret_dict['scg'] is None:
            ########################
            # Choose by image case #
            ########################
            # Get the priority list of SCGs that are applicable first,
            # then pick the first one. Filter on the given host if any.
            volume_refs = su.get_volumes_from_image(image,
                                                    continue_on_error=True,
                                                    msg_dict=ret_dict)
            try:
                scgs = self.get_scgs_for_volumes(context, volume_refs,
                                                 host=host, msg_dict=ret_dict)
            except Exception as ex:
                # msg_dict already contains the messages. Just continue
                scgs = []
            if scgs:
                LOG.debug("Picked an SCG by image. Name=%s, id=%s." %
                          (scgs[0].display_name, scgs[0].id))
                ret_dict['scg'] = scgs[0]
                try:
                    self.set_scg_to_instance(context, instance, scgs[0],
                                             ret_dict)
                except Exception as ex:
                    LOG.exception(ex)
                    ret_dict['messages'].append(_('%s') % ex)
        elif ret_dict['scg'] is None and instance:
            ###########################
            # Choose by instance case #
            ###########################
            # We need to pick an SCG based on the instance.
            # This is the onboarded VM case (not deployed from this PVC).
            scg = self.pick_scg_by_instance(instance, context, ret_dict)
            if scg is not None:
                ret_dict['scg'] = scg
                self.set_scg_to_instance(context, instance, scg, ret_dict)

        # 9267: We do not verify the 'enabled' state of the SCG here. We
        #       assume the enabled state is filtered on when choosing an
        #       SCG for deployment, but does not restrict operations later

        # jsonutils does not do well with unicode strings which have now
        # converted to Message types in openstack, so convert them back here
        ret_dict['messages'] = [_('%s') % x for x in ret_dict['messages']]
        LOG.debug("**EXIT** determined_scg='%s', returns: %s." %
                  ('-' if not ret_dict['scg'] else
                   ret_dict['scg'].display_name, ret_dict))
        if ret_dict['scg'] is None and raise_err:
            messages = ret_dict['messages']
            msg = stgex.IBMPowerVCSCGNotDetermined.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCSCGNotDetermined(msg)
        return ret_dict

    def pick_scg_by_instance(self, instance, context, msg_dict):
        """
        Need to pick a suitable SCG based on the specified on-boarded
        instance. So the instance has already been determined to not be
        directly associated with an SCG, either through its flavor or
        through its system_metadata properties. We want to be very specific
        with the steps that are followed here:

        1. Retrieve the block device mapping (BDM) table for the instance.
        2. Retrieve the LPAR IDs for the VIOSes this instance is using.
        3. Retrieve priority list of enabled SCGs by BDM connectivity type.
        4. Loop over enabled SCGs:
        4.1  Pick SCG if all LPAR IDs are within SCG membership and the
             FC connectivity matches.
        5. Return None if no match

        :param instance: The VM instance object to pick SCG for.
        :param context: The authorization context
        :parm msg_dict: A dictionary with a message list value that can
                        be appended to in the event that no SCG can be used.
        :returns: An SCG DOM object or None if one cannot be found.
        """
        bdmt = conductor.PowerVCConductorAPI().\
            block_device_mapping_get_all_by_instance(context, instance)
        cluster = None
        fc_access = False
        for bdm in bdmt:
            LOG.debug("block_device_mapping = %s." % bdm)
            conn_info = None
            if "connection_info" in bdm.keys():
                conn_info = bdm["connection_info"]
                if conn_info is not None and not isinstance(conn_info, dict):
                    conn_info = jsonutils.loads(str(conn_info))
                    LOG.debug("Converted connection_info: %s." % conn_info)
            if (conn_info is not None and
                    'driver_volume_type' in conn_info and
                    conn_info['driver_volume_type'] == CONNECTION_TYPE_SSP):
                # need to get the provider host name from the volume
                vol_id = conn_info['serial']
                cinder_api = pvc_cinder.PowerVCVolumeAPI()
                # following call will raise an exception if any issue.
                try:
                    provider = cinder_api.get_stg_provider_info(
                        context, volume_id=vol_id, use_cache=True)
                    cluster = provider['storage_hostname']
                except stgex.IBMPowerVCProviderError as p_err:
                    # Problem getting provider info or cinder service could be
                    # down. Re-raise.
                    LOG.error(_("Cinder volume service error trying to get "
                                "provider information for volume with ID '%("
                                "vol_id)s'. Error is: %(p_err)s") % locals())
                    raise
                except NotFound as err_404:
                    # If it's not a provider error, then the likely problem is
                    # that the volume is no longer managed. Return None.
                    LOG.exception(err_404)
                    msg = _("Unable to lookup volume ID '%(vol_id)s' for "
                            "server with ID '%(serv_id)s'. The error is: "
                            "%(err)s") % dict(vol_id=vol_id,
                                              serv_id=instance['id'],
                                              err=str(err_404))
                    LOG.warn(msg)
                    msg_dict['messages'].append(msg)
                    return None
                # Other error types bubble up
            else:
                # We assume that if the connection type is not for an ssp,
                # then it is for external FC volumes, even if the
                # connection_info is not known
                fc_access = True
        # Get the vios ids (lpar_ids)
        try:
            power_specs = pvcdb.instance_power_specs_find(
                context, dict(instance_uuid=instance['uuid']))
        except Exception as ex:
            LOG.exception(ex)
            power_specs = None
        power_specs = power_specs if power_specs else {}
        vios_ids = power_specs.get('vios_ids')
        vios_ids = vios_ids.split(',') if vios_ids else []
        if not vios_ids:
            msg = _("Warning: The instance with id '%s' is not reporting the "
                    "use of any Virtual I/O Server LPARs." % instance['uuid'])
            msg_dict['messages'].append(msg)
        # convert list of lpar_ids to vios_ids used by SCG infrastructure.
        vios_ids = [instance['host'] + "##" + x for x in vios_ids]
        LOG.info(_("Instance '%(inst)s' has block device mapping for cluster "
                   "'%(cluster)s' and uses VIOS IDs '%(vios)s' and registered "
                   "storage controller access is '%(access)s'.") %
                 dict(inst=instance['uuid'], cluster=cluster, vios=vios_ids,
                      access=str(fc_access)))
        # Now get the initial candidate list of SCGs in priority order
        # 9267: Request disabled SCGs in returned lists.
        if cluster:
            scgs = su.query_scgs(context, sort_type="cluster",
                                 enabled_only=False, cluster_provider=cluster,
                                 give_objects=True)
            if not scgs:
                msg = _("There are no candidate storage connectivity groups "
                        "where the Virtual I/O Server cluster is '%s'.")
                LOG.warn(msg % cluster)
                msg_dict['messages'].append(msg % cluster)
                return None
        else:
            # Pass a special value to say we don't want SCGs that are
            # associated with a cluster, because the existing NPIV disk(s)
            # may be connected through VIOSes not in a found cluster.
            scgs = su.query_scgs(context, sort_type="external",
                                 enabled_only=False, cluster_provider="|none|",
                                 give_objects=True)
            if not scgs:
                msg = _("There are no storage connectivity groups "
                        "with registered storage controller access.")
                LOG.warn(msg)
                msg_dict['messages'].append(msg)
                return None

        # Loop over the SCGs in priority order looking for the first match.
        # inst_vios_ids = set(vios_ids)
        for scg in scgs:
            # 9267: Force SCG match to the 'auto-defined' SCG that came
            # back as it has the maximal set of VIOSes. In the future, the
            # user should be able to choose the SCG for an onboarded VM.
            # The auto-defined SCG is used even if it is disabled. Also,
            # disabled ports are ignored.
            # Commenting code that checks VIOS subset membership, though
            # we still retrieve the VIOSes of the VM (above) for logging
            # purposes.
            #   if inst_vios_ids.issubset(scg.vios_ids) and
            #         (scg.fc_storage_access or not fc_access):
            if scg.auto_defined:
                msg = _("Auto-defined storage connectivity group '%(scg)s' "
                        "is matched to server instance '%(vm)s' with ID "
                        "%(uuid)s.")
                LOG.info(msg % dict(scg=scg.display_name, vm=instance['name'],
                                    uuid=instance['uuid']))
                return scg
        msg = (_("There are no storage connectivity groups with "
                 "the needed connectivity for instance id '%(inst)s' and "
                 "that have a superset of its Virtual I/O Server members "
                 "(<host>##vios_lpar_id,...): %(vios)s")
               % dict(inst=instance['uuid'], vios=vios_ids))
        msg_dict['messages'].append(msg)
        return None

    def set_scg_to_instance(self, context, instance, scg, msg_dict):
        """
        Set the id for the given SCG in the instance system_metadata.
        If there was a prior SCG id set, then return the old id, else
        return None.
        """
        if not instance:
            LOG.debug("No instance, so cannot persist the SCG for this op.")
            return None
        uuid = instance['uuid']
        if scg:
            scg_id = scg.id
        else:
            scg_id = None
        key = 'storage_connectivity_group_id'
        LOG.debug("Attempt to persist scg id '%s' to instance '%s'." %
                  (scg_id, uuid))
        old_scg_id = None
        sys_metadata = {}  # init for exception logging
        try:
            smd = db_api.instance_system_metadata_get(context, uuid)
            sys_metadata = smd if smd is not None else {}
            LOG.debug("pre system_metadata = %s." % sys_metadata)
            if key in sys_metadata.keys():
                old_scg_id = sys_metadata[key]
            sys_metadata[key] = scg_id  # add in the scg

            if old_scg_id is not None and old_scg_id == scg_id:
                LOG.debug(key + " is already in system_metadata. Nothing "
                          " to do.")
            else:
                LOG.debug("updated system_metadata to save = %s."
                          % sys_metadata)
                db_api.instance_system_metadata_update(context, uuid,
                                                       sys_metadata, False)
                if old_scg_id is not None and old_scg_id != scg_id:
                    msg = _("Warning: Overwrote Storage Connectivity Group "
                            " id '%(old_id)s' with '%(new_id)s' on "
                            "instance sys_metadata.") %\
                        dict(old_id=old_scg_id, new_id=scg_id)
                    msg_dict['messages'].append(msg)
            LOG.debug("Added system_metadata storage_connectivity_group_id")
        except Exception as ex:
            LOG.exception(ex)
            msg = _("Could not save the metadata to the instance: %s.")
            msg_dict['messages'].append(msg % sys_metadata)

        LOG.debug(_('%s') % msg_dict)
        return old_scg_id

    def get_scg_from_instance_meta(self, context, instance, msg_dict):
        """
        Return the SCG that is associated with the passed instance if one
        is referenced in its sys_metadata, else None.
        """
        uuid = instance['uuid']
        key = 'storage_connectivity_group_id'
        LOG.debug("Check instance sys_metadata for '%s'." % uuid)
        sys_metadata = db_api.instance_system_metadata_get(context, uuid)
        LOG.debug("system_metadata = %s." % sys_metadata)
        if sys_metadata is not None and key in sys_metadata.keys():
            scg_id = sys_metadata[key]
            if scg_id:
                try:
                    scg = su.get_scg_by_id(context, scg_id)
                    return scg
                except Exception as ex:
                    msg = _("Instance sys_metadata error. "
                            " Storage Connectivity Group for metadata "
                            " not found: %s")
                    msg_dict['messages'].append(msg % ex)
        msg_dict['messages'].append(_("Server %s has no storage "
                                      "connectivity group reference"
                                      " information in database.") % (uuid))
        LOG.debug(_('%s') % msg_dict)
        return None

    def get_scgs_for_volumes(self, context, volume_refs, host=None,
                             msg_dict=None):
        """
        This function encapsulates the core logic for the API to return
        a list of storage connectivity groups that are applicable for an
        image, but it operates on volumes so that other flows can use it
        as well.
        :param context: The authorization context for cinder and db access
        :param volume_refs: List of volume reference dicts to iterate over and
                            build an intersected set of SCGs. Each entry
                            contains the following keys: id, is_boot,
                            image_id, image_name
        :param host:    optional parameter - if passed, then the candidate
                        SCGs will be further constrained by those that have
                        the given host name as a member.
        :param msg_dict: Optional parameter that is a dictionary with a
                        'messages' key. The value is a list that can be
                        appended to with a new message - i.e. a possible
                        problem that does not get raised as an exception.
        :returns: A list of SCG DOM objects that can provide connectivity
                  using any host or the specific host specified.
        """
        # Call volume API to get the storage type for the volume
        cinder_api = pvc_cinder.PowerVCVolumeAPI()
        scg_list = []

        ################################################################
        # Loop over volumes, building an SCG list.
        #
        #   The processed_types and processed_ssp_providers sets are used
        #   to keep track of the storage type (external or cluster) and
        #   the cluster providers seen so far, respectively.
        ################################################################
        processed_types = set()
        processed_ssp_providers = set()
        for vol_ref in volume_refs:
            vol_id = vol_ref['id']
            try:
                provider_info = cinder_api.get_stg_provider_info(
                    context, volume_id=vol_id, use_cache=True)
            except stgex.IBMPowerVCProviderError as p_err:
                # Problem getting provider info or cinder service could be
                # down. Re-raise.
                LOG.error(_("Cinder volume service error trying to get "
                            "provider information for volume with ID "
                            "'%(vol_id)s'. Error is: %(p_err)s") % locals())
                raise
            except NotFound as err_404:
                # If it's not a provider error, then the likely problem is
                # that the volume is no longer managed.
                LOG.exception(err_404)
                msg = _("Unable to lookup volume ID '%(vol_id)s' for image "
                        "'%(image_id)s'. The error is: %(err)s") %\
                    dict(vol_id=vol_id, image_id=vol_ref['image_name'],
                         err=str(err_404))
                LOG.warn(msg)
                if msg_dict:
                    msg_dict['messages'].append(msg)
                # We could treat the boot volume case as the only hard error
                # here, but it seems if an attached volume also cannot be
                # found, then it may be for a different storage type that
                # would cause SCGs to be precluded from the returned list if
                # it was processed normally.
                raise exception.VolumeNotFound(message=msg)
            # Other error types bubble up

            provider_name = provider_info['storage_hostname']
            stg_type = provider_info['stg_type']
            # Note that the contract of get_stg_provider_info() ensures that
            # 'stg_type' is either 'cluster' or 'fc'
            if stg_type == "cluster":
                if (processed_ssp_providers and
                        provider_name not in processed_ssp_providers):
                    msg = _("The image or instance has volumes coming from "
                            "two different Virtual I/O Server clusters, "
                            "which is not supported. The volumes are: %s.")
                    error = msg % volume_refs
                    LOG.error(error)
                    msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
                    raise stgex.IBMPowerVCStorageError(msg)
                # else remember the cluster provider name
                processed_ssp_providers.add(provider_name)
            # If we have seen this storage type before, then we've
            # already included the candidate SCGs for it.
            if stg_type in processed_types:
                LOG.debug("We've already filtered on storage type '%s' "
                          "and have what we need." % stg_type)
                continue
            processed_types.add(stg_type)
            # Do the sorted query for proper storage type.
            # Note: It's possible that other types besides 'fc' could map
            #       to 'external in the future.
            sort_type = "external" if stg_type == "fc" else "cluster"
            # Only get enabled SCGs as well.
            #######################################
            # Internal API call to query SCGs     #
            #######################################
            provider = provider_name if sort_type == "cluster" else None
            cur_list = su.query_scgs(context,
                                     sort_type=sort_type,
                                     enabled_only=True,
                                     cluster_provider=provider,
                                     give_objects=True)
            if not cur_list and msg_dict is not None:
                msg = _("There are no storage connectivity groups (SCGs) "
                        "that map to the volume with id '%(vol_id)s'. "
                        "Searched for enabled SCGs of type %(type)s "
                        "associated with cluster provider '%(prov)s'.") %\
                    dict(vol_id=vol_id, type=sort_type, prov=provider)
                msg_dict['messages'].append(msg)
            if scg_list:
                # The new list is the intersection of scg_list and cur_list,
                # however, list order must be maintained per API specs, so
                # we cannot use a python set.
                cur_ids = [x.id for x in cur_list]
                LOG.debug("Running list: %s, cur_list: %s." %
                          (scg_list, cur_list))
                # Now take the intersection and maintain order of running list
                scg_list = [x for x in scg_list if x.id in cur_ids]
            else:
                # scg_list is empty so just assign to cur_list
                scg_list = cur_list
            LOG.debug("Running SCG list is now '%s'." % scg_list)
        # End for each volume loop
        #############################################################
        # If a host was passed in, then filter the final list by    #
        # those SCGs that contain the host                          #
        #############################################################
        name_list = [scg.display_name for scg in scg_list]
        if host and scg_list:
            scg_list = [x for x in scg_list if host in x.get_host_name_list()]
            LOG.debug("SCG list after including only those with host '%s' "
                      "is: '%s'." % (host, scg_list))
            if not scg_list and msg_dict is not None:
                msg = _("Of the applicable storage connectivity groups "
                        "(%(scg)s) for the image volume(s), none contained "
                        "host '%(host)s' as a member.") %\
                    dict(scg=name_list, host=host)
                msg_dict['messages'].append(msg)
            name_list = [scg.display_name for scg in scg_list]
        ###############################################################
        # Lastly, verify connectivity of the SCG for the host or any  #
        # member host. Remove those without connectivity.             #
        ###############################################################
        scg_list = [scg for scg in scg_list if
                    scg.verify_host_connectivity(context, host, msg_dict)]
        if name_list and not scg_list and msg_dict:
            if host:
                hmsg = _("currently provides connectivity through member "
                         "host '%s'.") % host
            else:
                hmsg = _("has a member host that currently provides "
                         "connectivity.")
            msg = _("None of the applicable storage connectivity groups "
                    "(%(scg)s) for the image volume(s) %(msg)s") %\
                dict(scg=name_list, msg=hmsg)
            msg_dict['messages'].append(msg)
        return scg_list

    def get_instances_for_scg(self, context, scg, msg_dict):
        """
        Check Storage Connectivity Group and return all instances that
        reference it using instance meta reference to SCG. This assume
        that the meta data relationship from instance to SCG by the SCG ID
        is the final authority for what SCG is related to instances
        :param context: A context object that is used to authorize
                    any DB access.
        :param scg: SCG object that is checked to see if associated with
                    any instances
        :returns: list of instances that references the SCG by the instance
                  meta data
        """
        LOG.debug(" ---> Find any instances associated with SCG using "
                  "instance meta data %s" % str(scg))
        # get scg's id
        scg_id = scg.id
        ref_instances = []

        # get all known instances
        instances = db.instance_get_all(context)
        LOG.debug("Check %s instances" % len(instances))
        if instances:
            # there are instances in list
            LOG.debug(" >>>> Looking at instances SCG meta ID "
                      "  equal to SCG id= %s"
                      % scg_id)
            for instance in instances:
                # get instance scg ref ID from instance meta
                scg_dom_obj = self.get_scg_from_instance_meta(context,
                                                              instance,
                                                              msg_dict)
                if scg_dom_obj is not None:
                    scg_meta_id = scg_dom_obj.id
                    LOG.debug(" >>>> Instance SCG meta ID is %s "
                              % scg_meta_id)
                    #if instance scg reg ID is SCG looking for then
                    if scg_id == scg_meta_id:
                        #add instance to list
                        ref_instances.append(instance)
        else:
            LOG.debug(" There were NO instances at all to check. ")
        LOG.debug(" <--- Found %s instances associated with SCG %s"
                  % (len(ref_instances), scg.id))
        return ref_instances

    def get_providers_by_instance(self, context, instance_uuid,
                                  instance=None):
        """
        Given a VS instance, lookup the storage connectivity group (SCG)
        for the instance and then return viable (candidate) cinder hosts that
        storage could be allocated from for this instance, based on the
        storage connectivity specification. The SCG must provide connectivity
        to the storage provider and the provider must not be in error state
        for it to be in the returned dictionary set.

        :param context: A context object that is used to authorize
                        DB access and cinder API calls.
        :param instance_uuid: The instance UUID that will be used to
                        get the storage requirements of an instance (SCG)
        :param instance: Optional instance object if it is available.
                        If passed, that object will be used directly rather
                        than looking it up through the instance_uuid.
        :returns: A dictionary with an entry for a list of 'storage-hosts'.
                        { 'storage-hosts': [
                            {
                                'host': <storage_hostname>
                            },...
                          ],
                          'messages': [<optional list of informational msgs>]
                        }
        """
        providers = {'storage-hosts': []}
        if not instance:
            # Retrieve instance if not provided
            instance = db.instance_get_by_uuid(context, instance_uuid)
        host = instance.get('host')

        ###################################################
        # Use common method entry to determine the SCG.   #
        # Case:                                           #
        #    pre-attach --> "storage-hosts"(API) -->      #
        #        get_cinder_hosts_by_instance(instance)   #
        ###################################################
        LOG.debug("Calling determine_scg() for the pre-attach case.")
        response = self.determine_scg(context, instance=instance, host=host)
        if response['scg'] is None:
            providers['messages'] = response['messages']
        else:
            # The cinder hosts are already obtained and cached in the
            # dictionary representation of the SCG
            scg_dict = response['scg'].to_dict()
            scg_name = scg_dict['display_name']
            if 'applicable_providers' in scg_dict:
                provider_names = scg_dict['applicable_providers']
            else:
                msg = stgex.IBMPowerVCGetProviderError.msg_fmt % locals()
                LOG.error(msg)
                providers['messages'] = [msg]

            if provider_names:
                providers["storage-hosts"] = [{'host': x} for x in
                                              provider_names]
            else:
                msg = stgex.IBMPowerVCNoProviders.msg_fmt % locals()
                providers['messages'] = [msg]
                LOG.debug(msg)

            LOG.debug("**EXIT** SCG='%s', providers: %s." %
                      (scg_name, providers))
        return providers

    def get_image_from_instance(self, context, instance):
        """
        Return the 'image' looked up through the reference on the instance.
        """
        image_id = None
        if instance.get('image_ref'):
            image_id = instance['image_ref']
        elif instance.get('image'):
            image_id = instance["image"]["id"]
        else:
            image_msg = _("No image was found for instance uuid '%s'.")
            LOG.info(image_msg % instance["uuid"])
        if image_id:
            if image_id == "ONBOARDED_NO_IMAGE":
                LOG.info(_("Special image id for onboarded VM case. "
                           "Skipping image lookup."))
                return None
            try:
                # make sure this is not just the 'slim' admin context
                if context.to_dict()["project_id"] is not None:
                    image = self.image_service.show(context, image_id)
                    LOG.debug("Image id '%s' looked up from instance."
                              % image_id)
                    return image
                else:
                    LOG.warn(_("Do not have adequate 'context' to look "
                               "up the image for id '%s'.") % image_id)
            except Exception as ex:
                LOG.exception(ex)
                msg = _("Could not look up image id '%(image_id)s' for "
                        "instance id '%(inst_id)s': %(ex)s.")
                image_msg = msg % dict(image_id=image_id,
                                       inst_id=instance["uuid"], ex=ex)
                LOG.info(image_msg)
        return None

    def get_scg_volume_types(self, context, scg, msg_dict):
        """
        Return a list of volume_types associated with the SCG passed in.
        1. Get list of applicable storage providers from the SCG.
        2. Calls the following API to get all the storage volume types:
           GET <cinder-URL>/types
        3 Loops over the volume types
          If the type['extra_specs']['capabilities:volume_backend_name']
          is in SCG provider list, then add this volume-type to the
          volume types list for return, else skip it.
        """
        scg_dict = scg.to_dict(context=context)
        if 'applicable_providers' in scg_dict:
            provider_names = scg_dict['applicable_providers']
        else:
            scg_name = scg.display_name
            msg = stgex.IBMPowerVCGetProviderError.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCGetProviderError(msg)
        # Do we have any providers? Fail if not
        if not provider_names:
            scg_name = scg.display_name
            msg = stgex.IBMPowerVCNoProviders.msg_fmt % locals()
            msg_dict['messages'].append(msg)
            LOG.info(msg)
            return None

        #get all possible volume types
        volume_api = pvc_cinder.PowerVCVolumeAPI()
        volume_types_list = volume_api.list_volume_types(context)
        if volume_types_list:
            LOG.debug("Found %s possible volume types when looking for"
                      " volume types for scg_id=%s. "
                      % (len(volume_types_list), scg.id))
        else:
            msg = _("Unable to find any storage templates (volume types)."
                    "Default storage templates do not exist for registered "
                    "providers.")
            msg_dict['messages'].append(msg)
            LOG.warn(msg)
            return None

        # filter types by applicable providers
        vol_types = [vt for vt in volume_types_list if
                     vt['extra_specs']['capabilities:volume_backend_name'] in
                     provider_names]

        if not vol_types:
            msg = _("Unable to find any volume types for storage providers "
                    "applicable to storage connectivity group '%(scg)s'. The "
                    "provider IDs are: %(prov_ids)s.") %\
                dict(scg=scg.id, prov_ids=provider_names)
            LOG.info(msg)
            msg_dict['messages'].append(msg)
            LOG.debug("Full dump of volume types: %s" % volume_types_list)
        LOG.debug("Returning volume types: %s." % vol_types)
        return vol_types

    def get_scg_volume_refs(self, context, scg, msg_dict):
        """
        Return a list of volumes associated with the SCG passed in.
        This is an indirect association. First we check which storage
        providers are 'accessible' by the SCG and are not in error state.
        Then we get volumes from the cinder service and filter them based
        on the owning provider. This method does not filter based on
        volume type or other attributes.
        """
        volumes = []
        volume_api = pvc_cinder.PowerVCVolumeAPI()
        scg_dict = scg.to_dict(context=context)
        # The applicable_providers list in the SCG dictionary contain the
        # providers that are not in error state.
        # Using the to_dict() method above saves us from having to
        # make a cinder API call again for these providers since all we
        # need is the name to match on later.
        if 'applicable_providers' in scg_dict:
            provider_names = scg_dict['applicable_providers']
        else:
            scg_name = scg.display_name
            msg = stgex.IBMPowerVCGetProviderError.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCGetProviderError(msg)
        # Do we have any providers? Log a message if not.
        if not provider_names:
            scg_name = scg.display_name
            msg = stgex.IBMPowerVCNoProviders.msg_fmt % locals()
            msg_dict['messages'].append(msg)
            LOG.info(msg)
            return volumes

        # Get list of volumes. Let exceptions bubble up.
        LOG.debug("volume_api.get_all() to get vol data for all.")
        vol_list = volume_api.get_all(context)
        LOG.debug("Provider list = %s. Original volume list = %s." %
                  (provider_names, vol_list))
        # Filter volumes by their provider key
        volumes = [v for v in vol_list
                   if v[pvc_cinder.PROVIDER_KEY] in provider_names]

        LOG.debug("*** Returning volumes list for SCG ID '%s'. List size "
                  "is %d." % (scg.id, len(volumes)))
        return volumes

    @logcall
    def get_hmc_migrate_map_strings(self, context, client_vfc_list,
                                    client_vscsi_list,
                                    instance, dest_host_name):
        """
        During an HMC LPAR migration, this method will get called to
        generate the virtual_fc_mappings and/or virtual_scsi_mappings strings
        that the HMC migrlpar command accepts (and by extension, the K2 API)
        to tell the HMC the preferences for how we want the migrated LPAR's
        virtual FC/vSCSI port/slot mappings to end up. We want them to end
        up in the bounds of the storage connectivity group (SCG) for the
        'instance'. This method will map each client (VM) virtual slot to
        a target VIOS LPAR id (and optionally, physical FC port), such that
        the target VIOS is within the SCG, the target physical port is
        within the SCG, and the fabric mappings are maintained (i.e. if port
        x goes to fabric  A on source system, then that same port/slot
        needs to be mapped to a fabric A port on the target). Virtual SCSI
        mappings only need a target VIOS partition id.

        :param context: The authenticaion context.
        :param client_vfc_list: List of dictionaries corresponding to each
                    client adapter port mapping. Single entry example:
                        [{'vios_id': '1', 'phys_wwpn': '10000090FA2A54CC',
                          'client_slot': '4'}]
                    This says that virtual server slot 4 is mapped through
                    remote VIOS partition id 1 and is associated with the
                    physical FC Port that has WWPN 10000090FA2A54CC. We
                    Need to map this client slot to a different VIOS and
                    FC port on the destination host.
                    This list will be empty if there are no FC disks attached.
        :param client_vscsi_list: List of dictionaries corresponding to each
                    vSCSI mapping for the instance. Single entry example:
                        [{'vios_id': '1', 'client_slot': '5'}]
                    This says that the virtual server has client slot 5
                    mapped into remote VIOS partition id 1. We need to map
                    slot 5 to a VIOS partition on the destination host.
                    This list will be empty if there are no SSP disks attached
        :param instance: The VM instance object/dict
        :parm dest_host_name: Host name that the VM is scheduled to migrate
                   to. The source host is asssumed to be instance['host'].
        :returns:  A dictionary of containing the connectivity type
                   mapping strings that are applicable to the instance.
                   The value None will be returned for a connectivity type
                   mapping string if an override option should not be given
                   to the HMC (let HMC choose the mapping). Format:
                       { 'npiv': "virtual-slot-number//vios-lpar-ID//
                                  vios-fc-port-name,...",
                         'ibm_ssp': "virtual-slot-number//vios-lpar-ID,..."
                       }
                   For example:
                       { 'npiv': "4//1//fcs0,5//2//fcs1",
                         'ibm_ssp': "6//1" }
            Notes:
                *If no SCG can be determined for the instance or if there
                 is no valid mappings that stay within the bounds of the
                 SCG, then a failure will be bubbled up from the
                 get_connectivity_info flow.
                *If a client slot is not associated with a physical FC
                 port, then a mapping for that client slot/port will not
                 be included in the returned list.
        """
        if instance['host'] == dest_host_name:
            error = _("The destination host for migration of the virtual "
                      "machine is the same as its current host. The "
                      "hypervisor host name is %s") % dest_host_name
            msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCStorageError(msg)

        # Obtain the connectivity info for the destination host.
        # Do not use 'live_data' for these calls for performance.
        dest_conn = self.get_connectivity_info(context, instance['uuid'],
                                               dest_host_name, instance,
                                               live_data=False)
        # default return dictionary
        string_mappings = {CONNECTION_TYPE_NPIV: None,
                           CONNECTION_TYPE_SSP: None}

        ########################################################
        # Check if we need to do the FC/NPIV mappings.         #
        # If not, then just let the HMC decide the mappings.   #
        ########################################################
        default_maps_ok = su.check_if_hmc_default_migrate_maps_ok(context,
                                                                  dest_conn)
        if client_vfc_list and not default_maps_ok[CONNECTION_TYPE_NPIV]:
            # Get the fabric used for the source host VM ports.
            # To do this we just call a helper to pull the fabric metadata
            # from the database for a port's wwpn.
            wwpns = [vfc['phys_wwpn'] for vfc in client_vfc_list]
            wwpn_to_fabric = su.get_db_wwpns_to_fabric_map(context, wwpns)
            LOG.debug("\nSource host '%s' fabrics: %s\nDestination "
                      "host '%s' connectivity: %s\n" %
                      (instance['host'], wwpn_to_fabric, dest_host_name,
                       dest_conn))

            mapping_list = []  # initialize var to contain mappings
            # The following dictionary is used to keep track of the mapping of
            # source vios lpar IDs to a destination vios dictionary with
            # a list of ports already mapped:
            #     {'dest-vios-lpar_id': '5', 'ports': [<port names>..]}
            #
            # Note: We don't need to track which client slot maps to which port
            vios_mapping = {}
            # Loop over the client vfcs
            for client_vfc in client_vfc_list:
                if not client_vfc['phys_wwpn']:
                    LOG.debug("Skipping client adapter slot entry because "
                              "there is no physical port: %s." % client_vfc)
                    continue

                # Call a helper to find the best/next matching destination port
                vios, port_name = self._get_matching_dest_port(
                    client_vfc, dest_conn,
                    wwpn_to_fabric[client_vfc['phys_wwpn']], vios_mapping)
                # If vios is None, then there is no available mapping to be
                # had using the information that we know about.
                if not vios:
                    wwpn = client_vfc['phys_wwpn']
                    scg_name = dest_conn['scg_name']
                    msg = stgex.IBMPowerVCStorageMigError.msg_fmt % locals()
                    LOG.error(msg)
                    raise stgex.IBMPowerVCStorageMigError(msg)

                # Add mapping to what is being tracked. The 'vios_id' is the
                # lpar_id of the vios.
                if client_vfc['vios_id'] in vios_mapping:
                    vmap = vios_mapping[client_vfc['vios_id']]
                    vmap['ports'].append(port_name)
                else:
                    vmap = {'dest-vios-lpar_id': vios, 'ports': [port_name]}
                    vios_mapping[client_vfc['vios_id']] = vmap

                # Now add the mapping to the string we are building up.
                mapping = client_vfc['client_slot'] + "//" + vios + "//" + \
                    port_name
                LOG.debug("Mapping %s to string %s." % (client_vfc, mapping))
                mapping_list.append(mapping)
            # end loop
            string_mappings[CONNECTION_TYPE_NPIV] = ','.join(mapping_list)

        ########################################################
        # Check if we need to do the VSCSI mappings for SSP.   #
        # If not, then just let the HMC decide the mappings.   #
        ########################################################
        if client_vscsi_list and not default_maps_ok[CONNECTION_TYPE_SSP]:
            string_mappings[CONNECTION_TYPE_SSP] = \
                self._get_ssp_migrate_maps(context, dest_conn,
                                           client_vscsi_list, instance)

        # log and return
        LOG.info(_("Returning migration mappings '%(mappings)s' for instance "
                   "id '%(inst_id)s' to destination host '%(dest_host)s'. "
                   "Input client virtual mappings: vfcs=%(vfc)s, "
                   "vscsis=%(vscsi)s.") %
                 dict(mappings=string_mappings, inst_id=instance['uuid'],
                      dest_host=dest_host_name, vfc=client_vfc_list,
                      vscsi=client_vscsi_list))
        return string_mappings

    @logcall
    def _get_matching_dest_port(self, vfc_dict, dest_conn, fabric,
                                vios_mapping):
        """
        Helper method inspect the dest_conn (SCG connectivity information
        for the destination host) to pull out the next best matching
        port for the given vfc. The fabric param is the fabric to match
        against and vios_mapping is a dictionary of mappings already done,
        so that we don't choose target ports already mapped.
        Example data:
          fabric -    "A"
          vfc_dict -  {'vios_id': '1', 'phys_wwpn': '10000090FA2A54CC',
                       'client_slot': '4'}
          dest_conn - {'connection-types': ['npiv', 'ibm_ssp'],
                       'vios_list':
                         [{'name': 'acme108', 'lpar_id': 1,
                           'connection-types': ['npiv'],
                           'ports': {'A':
                             [{'udid': '1aU78AE.001.WZS02AX-P1-C19-L1-T1',
                               'wwpn': '21000024FF418248', 'status': 'OK',
                               'name': 'fcs0'}]}}
                         ]}
          vios_mapping - {'1': {'dest-vios-lpar_id': '2',
                                'ports': ['fcs0', 'fcs1'] } }
        """
        client_slot = vfc_dict['client_slot']
        source_id = vfc_dict['vios_id']  # the lpar id
        # Create a list of target candidate vios dictionaries from the
        # connectivity info passed in and the mappings that we have
        # already established.
        if source_id in vios_mapping:
            target_id = vios_mapping[source_id]['dest-vios-lpar_id']
            mapped_port_names = vios_mapping[source_id]['ports']
            candidates = [x for x in dest_conn['vios_list'] if
                          str(x['lpar_id']) == target_id]
        else:
            # need to map to a VIOS that hasn't been mapped, otherwise
            # it could cross map for dual vios config.
            alreadyusedvios = [x['dest-vios-lpar_id'] for x in
                               vios_mapping.values()]
            candidates = [x for x in dest_conn['vios_list'] if
                          str(x['lpar_id']) not in alreadyusedvios and
                          CONNECTION_TYPE_NPIV in x['connection-types']]
            mapped_port_names = []
        # Loop over candidate vios connectivity dictionaries
        for vios_dict in candidates:
            current_id = str(vios_dict['lpar_id'])
            LOG.debug("vios_dict=%s." % vios_dict)

            # We match the first available port in same fabric
            if fabric in vios_dict['ports']:
                # Fabrics are configured consistently between hosts
                for port in vios_dict['ports'][fabric]:
                    if port['name'] not in mapped_port_names:
                        LOG.debug("Found a port match: %s." % port)
                        return current_id, port['name']
                LOG.debug("No ports on VIOS %s available on fabric %s."
                          % (current_id, fabric))
            else:
                # Fabrics are not conssitently configured across hosts.
                # We could match fabric "None" to "A" or "B", or fabric
                # A/B to "None". But for now, I take a conservative
                # approach and find no mapping for this vios.
                vios_name = vios_dict['name']
                LOG.warn(_("Source host client slot '%(client_slot)s' is "
                           "using a physical FC port on fabric %(fabric)s"
                           ", but that fabric is not configured for any "
                           "FC ports on the destination host VIOS "
                           "'%(vios_name)s'. Continuing.") % locals())
        LOG.debug("No FC mapping found for VFC %s." % vfc_dict)
        return None, None

    def _get_ssp_migrate_maps(self, context, dest_conn,
                              client_vscsi_list, instance):
        """
        Return a string of SSP connectivity migration maps for the instance,
        given the pre-migration client_vscsi_list and the already computed
        connectivity information for the destination host (dest_conn).
        Example input data:
            client_vscsi_list
                [{'client_slot': '5', 'vios_id': '1'},
                 {'client_slot': '6', 'vios_id': '2'}]
            dest_conn
                {'connection-types': ['npiv', 'ibm_ssp'],
                 'target_host': "8231E2D_109EFET",
                 'scg_name': "Any host in Shared Storage Pool cluster8",
                 'vios_list':
                    [{'name': 'acme108', 'lpar_id': 1,
                      'connection-types': ['npiv', 'ibm_ssp']},
                     {'name': 'acme108', 'lpar_id': 1,
                      'connection-types': ['npiv', 'ibm_ssp']}
                    ]
                }
        """
        ssp_map_str = None
        # mapping: source vios partition id --> destination vios partition id
        dest_maps = {}
        if not client_vscsi_list or 'vios_list' not in dest_conn:
            LOG.debug("Empty client_vscsi_list or vios_list.")
            return ""
        # We assume that the vios_list in the destination host connectivity
        # information is already sorted (if possible & applicable) in
        # a priority order based on the number of existing virtual storage
        # mappings. So, we just collect the partition ids in a list.
        vios_ids = [str(v['lpar_id']) for v in dest_conn['vios_list']
                    if CONNECTION_TYPE_SSP in v['connection-types']]
        client_slots = set()
        # Loop over the source client vscsi mappings passed in.
        for source_map in client_vscsi_list:
            LOG.debug("vios_ids - %s. dest_maps - %s, source_map - %s" %
                      (vios_ids, dest_maps, source_map))
            c_slot = source_map['client_slot']
            if c_slot in client_slots:
                LOG.debug("Already mapped client_slot %s. Continue." % c_slot)
                continue
            if source_map['vios_id'] in dest_maps:
                vmap = c_slot + "//" + dest_maps[source_map['vios_id']]
            elif vios_ids:
                # pop the first one off the list
                vmap = c_slot + "//" + vios_ids[0]
                dest_maps[source_map['vios_id']] = vios_ids[0]
                del vios_ids[0]
            else:
                dest_host_name = dest_conn['target_host']
                scg_name = dest_conn['scg_name']
                needed = len(set([x['vios_id'] for x in client_vscsi_list]))
                msg = stgex.IBMPowerVCStorageMigError2.msg_fmt % locals()
                LOG.error(msg)
                raise stgex.IBMPowerVCStorageMigError2(msg)
            client_slots.add(c_slot)
            ssp_map_str = ssp_map_str + "," + vmap if ssp_map_str else vmap

        LOG.debug("Return SSP migration mapping string '%s'. Dest "
                  "VIOS partition IDs left: %s." % (ssp_map_str, vios_ids))
        return ssp_map_str
