# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================
"""
Handles all requests relating to volumes + cinder.
"""

from cinderclient import exceptions as cinder_exception
import copy
from datetime import datetime, timedelta
from nova import exception
from nova.openstack.common import log as logging
from paxes_nova import _
from nova.volume import cinder
import sys

from paxes_nova.storage import exception as stgex


PROVIDER_KEY = 'host'
CINDER_HOST_KEY = 'os-vol-host-attr:host'
LOG = logging.getLogger(__name__)


class PowerVCCinderConflict(cinder_exception.ClientException):
    """
    HTTP 409 - This indicates that the request could not be completed due
    to a conflict with the current state of the resource.
    """
    http_status = 409
    message = "Conflict"


class PowerVCCinderInternalServerError(cinder_exception.ClientException):
    """
    HTTP 500 -  This indicates that the cinder service is currently in
    error state.
    """
    http_status = 500
    message = "Internal Server Error"


class PowerVCCinderInsufficientStorage(cinder_exception.ClientException):
    """
    HTTP 507 - This indicates that the server does not have enough space to
    save the resource.
    """
    http_status = 507
    message = "Insufficient Storage"


class PowerVCExpendvdiskCapacityException(exception.NovaException):
    """
    The resize request exceeds the storage capacity
    """
    msg_fmt = _("%(error)s")


class PowerVCSVCVdiskNotFoundException(exception.NovaException):
    """
    The back end SVC volume has been deleted
    """
    msg_fmt = _("%(error)s")


class PowerVCExpendvdiskFCMapException(exception.NovaException):
    """
    The SVC volume can not be extended due to existing flashcopy mapping.
    """
    msg_fmt = _("%(errors)s")


class PowerVCExtendVolumeNotImplementedError(exception.NovaException):
    """
    The extend volume REST API is not implemented for certain volume
    driver.
    """
    msg_fmt = _("%(error)s")


class PowerVCVolumeServiceInternalError(exception.NovaException):
    """
    The extend-volume service had internal error when sending request
    to volume service.
    """
    msg_fmt = _("%(error)s")


def _untranslate_volume_summary_view(context, vol):
    """Maps keys for volumes summary view."""
    volume = cinder._untranslate_volume_summary_view(context, vol)
    # OSEE is broken on volume_type_id field. Fix it here.
    if "volume_type_id" in volume.keys():
        volume['volume_type'] = volume['volume_type_id']
        del volume['volume_type_id']
    if hasattr(vol, CINDER_HOST_KEY):
        volume[PROVIDER_KEY] = getattr(vol, CINDER_HOST_KEY)
    else:
        volume[PROVIDER_KEY] = None
    if hasattr(vol, 'source_volid'):
        volume['source_volid'] = vol.source_volid
    else:
        volume['source_volid'] = None
    # report volume health status too
    if hasattr(vol, 'health_status'):
        volume['health_status'] = copy.deepcopy(vol.health_status)
    else:
        volume['health_status'] = None
    return volume


def _untranslate_volume_type_view(volume_type):
    """ Maps keys for volume type summary view """
    t = {}
    t['id'] = volume_type.id
    t['name'] = volume_type.name
    t['extra_specs'] = volume_type.extra_specs
    return t


def translate_volume_exception(method):
    """Transforms the exception for the volume but keeps its traceback intact.
    """
    def wrapper(self, ctx, volume_id, *args, **kwargs):
        try:
            res = method(self, ctx, volume_id, *args, **kwargs)
        except cinder_exception.ClientException:
            exc_type, exc_value, exc_trace = sys.exc_info()
            vol_error = exc_value.message
            exc_reasons = vol_error.split(':')
            vol_exc_value = (exc_reasons[1] if len(exc_reasons) > 1
                             else vol_error)
            if isinstance(exc_value, cinder_exception.NotFound):
                if 'SVCVdiskNotFoundException' in vol_error:
                    exc_value = PowerVCSVCVdiskNotFoundException(
                        error=vol_exc_value)
                else:
                    exc_value = exception.VolumeNotFound(volume_id=volume_id)
            elif isinstance(exc_value, cinder_exception.BadRequest):
                exc_value = exception.InvalidInput(reason=exc_value.message)
            elif isinstance(exc_value, cinder_exception.HTTPNotImplemented):
                exc_value = exception.NotAllowed()
            elif isinstance(exc_value, PowerVCCinderInsufficientStorage):
                if "PVCExpendvdiskCapacityException" in vol_error:
                    exc_value = PowerVCExpendvdiskCapacityException(
                        error=vol_exc_value)
            elif isinstance(exc_value, PowerVCCinderConflict):
                if "NotImplementedError" in vol_error:
                    exc_value = PowerVCExtendVolumeNotImplementedError(
                        error=vol_exc_value)
                elif "PVCExpendvdiskFCMapException" in vol_error:
                    exc_value = PowerVCExpendvdiskFCMapException(
                        error=vol_exc_value)
                else:
                    # any other HTTPConflit error we don't know about
                    raise
            elif isinstance(exc_value, PowerVCCinderInternalServerError):
                exc_value = PowerVCVolumeServiceInternalError(
                    error=vol_exc_value)
            raise exc_value, None, exc_trace
        return res
    return wrapper


class PowerVCVolumeAPI(cinder.API):
    """ Extend base Nova volume API with PowerVC capabilities"""

    provider_info_cached = None
    cache_time = None
    """ provider_info_cached gets set to the output of get_stg_provider_info().
        It is not to be used for anything that requires up-to-date information.
        Nor should it be depended on for existing. There is no locking around
        this static variable.
        cache_time is the time that the cache info was populated. It times
        out after 1 min.
    """

    def __init__(self):
        """ Constructor for the PowerVC volume API extension"""
        super(PowerVCVolumeAPI, self).__init__()

    @translate_volume_exception
    def get(self, context, volume_id):
        item = cinder.cinderclient(context).volumes.get(volume_id)
        volume = _untranslate_volume_summary_view(context, item)
        return volume

    def get_all(self, context, search_opts={}):
        items = cinder.cinderclient(context).volumes.list(detailed=True)
        rval = []

        for item in items:
            rval.append(_untranslate_volume_summary_view(context, item))

        return rval

    def create(self, context, size, name, description, snapshot=None,
               source_vol=None, image_id=None, volume_type=None, metadata=None,
               availability_zone=None):

        if source_vol is not None:
            source_volid = source_vol['id']
        else:
            source_volid = None

        if snapshot is not None:
            snapshot_id = snapshot['id']
        else:
            snapshot_id = None

        kwargs = dict(source_volid=source_volid,
                      snapshot_id=snapshot_id,
                      display_name=name,
                      display_description=description,
                      volume_type=volume_type,
                      user_id=context.user_id,
                      project_id=context.project_id,
                      availability_zone=availability_zone,
                      metadata=metadata,
                      imageRef=image_id)

        item = cinder.cinderclient(context).volumes.create(size, **kwargs)
        return _untranslate_volume_summary_view(context, item)

    @translate_volume_exception
    def delete_volume(self, context, volume_id):
        """
        """
        cinder.cinderclient(context).volumes.delete(volume_id)

    @translate_volume_exception
    def extend_volume(self, context, volume_id, new_size):
        """
         increase volume size to the new_size(gigabytes).
         :param volume_id: The volume to extend
         :param new_size: The new volume size to extend to.
         :return: None
        """
        if not volume_id:
            return None

        client = cinder.cinderclient(context)
        client.volumes._action('ibm-extend',
                               volume_id,
                               {'new_size': new_size})

    def get_volume_type(self, context, voltype_id):
        """ Get volume type details based on volume type id
        :param volume_type_id: volume type id
        :param context: operation context
        :return: volume type name or None
        """
        if not voltype_id:
            return None

        volume_type =\
            cinder.cinderclient(context).volume_types.get(voltype_id)
        return _untranslate_volume_type_view(volume_type)

    def get_stg_provider_info(self, context, storage_hostname=None,
                              cinder_c=None, volume_id=None, use_cache=False,
                              exclude_in_error=False):
        """
        Get the storage provider information, including the stg_type for a
        provider, if specified, or for all registered storage providers.
        In this context, the type is one of two possibilities:
            cluster or fc.
        :param context:  A context object that is used to authorize
                         any DB or API access.
        :param storage_hostname: The storage provider to get the storage info
                         for. If None and volume_id is None, then return info
                         for all providers.
        :param cinder_c: If provided, this is the cinder client service
                         to issue GET calls against. If this is not
                         specified, then a new client will be created.
        :param volume_id: If provided, then return the single storage
                         provider that hosts the volume.
        :param use_cache: If a provider cache is available and is not timed
                         out, then pull info from the cache if this param
                         is True.
        :param exclude_in_error: If True, then provider(s) with a
                         backend_state of 'error' are excluded. This option
                         is ignored if storage_hostname is specified or if
                         volume_id is specified.
        :returns: A dict of provider information. This is the same info
                  as from the "storage-providers" REST API, but in a flat
                  dictionary. If storage_hostname is not specified, then the
                  provider dictionary is the value of the parent dictionary
                  keyed by provider name. Some of the interesting properties
                  are shown in the following example return format:
            {<provider-name>:
                {
                    "stg_type": <"fc"|"cluster">,
                    "host_display_name": <display-name>,
                    "backend_id": <k2-id>,
                    "backend_state": <running/stopped/...>,
                    ...
                },
             ...
            }
        Note that the stg_type is a mapping from the "backend_type" property
        (also available), where "ssp" maps to "cluster" and everything else
        maps to "fc". This may be expanded in the future, but today,
        storage connectivity groups only distinguish between these two types.
        """
        info = None
        # check cache
        if use_cache and PowerVCVolumeAPI.cache_time:
            d = datetime.now() - PowerVCVolumeAPI.cache_time
            if d < timedelta(0, 60, 0):  # under 1 min recent
                LOG.debug("Using cached provider info from: %s"
                          % str(PowerVCVolumeAPI.cache_time))
                info = PowerVCVolumeAPI.provider_info_cached
        if not info:
            if not context or not context.to_dict()["project_id"]:
                # This condition is normal for calls from the compute host
                # instead of nova-api, so make this a debug message - the
                # call is not necessary from compute host processes.
                # NOTE: Should no longer see this in 1.2.1+ as the periodic
                #       compute task's context was beefed up to be able to
                #       do REST API calls.
                LOG.debug("No authorization to make storage API call.")
                return None
            if cinder_c is None:
                cinder_c = cinder.cinderclient(context)
            try:
                urlpath = "/storage-providers/detail"
                info = cinder_c.client.get(urlpath)
            except Exception as e:
                LOG.exception(e)
                response = e.__class__.__name__ + " - " + _('%s') % e
                msg = stgex.IBMPowerVCProviderError.msg_fmt % locals()
                ex = stgex.IBMPowerVCProviderError(msg)
                raise ex
            # info will be returned as a tuple and we expect the second element
            # to contain the provider dictionary.
            if (len(info) != 2) or (not "storage_providers" in info[1]):
                LOG.warn(str(info))
                response = str(info[0])
                msg = stgex.IBMPowerVCProviderError.msg_fmt % locals()
                ex = stgex.IBMPowerVCProviderError(msg)
                LOG.exception(ex)
                raise ex
            LOG.debug("GET cinder 'storage-providers/detail' returns: %s"
                      % str(info))
            # update cache
            PowerVCVolumeAPI.provider_info_cached = info
            PowerVCVolumeAPI.cache_time = datetime.now()

        ################################
        # Handle specific volume case
        ################################
        if volume_id is not None:
            if not cinder_c:
                cinder_c = cinder.cinderclient(context)
            vol = cinder_c.volumes.get(volume_id)
            LOG.debug("Volume looked up for id '%s': %s" % (volume_id, vol))
            try:
                # The volume case now folds into the cinder host case
                storage_hostname = vol.__getattr__(CINDER_HOST_KEY)
            except Exception as ex:
                # Treat as an exception case now to avoid empty drop down
                # in GUI, with no explanation.
                LOG.execption(ex)
                error = str(_("Volume with id '%(vol_id)s' does not contain "
                              "the property '%(prop)s'. Ensure that the "
                              "volume has been onboarded as a PowerVC "
                              "cinder volume.")
                            % dict(vol_id=volume_id, prop=CINDER_HOST_KEY))
                msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
                raise stgex.IBMPowerVCStorageError(msg)

        provider_info = {}
        # Loop over returned providers
        for provider in info[1]["storage_providers"]:
            if "service" in provider:
                for key in provider["service"]:
                    provider[key] = provider["service"][key]
                del provider["service"]
            if "backend_type" in provider and\
                    provider["backend_type"] == "ssp":
                provider["stg_type"] = "cluster"
            else:
                provider["stg_type"] = "fc"
            if storage_hostname is not None and\
                    storage_hostname == provider["storage_hostname"]:
                return provider
            if exclude_in_error and provider['backend_state'] != 'error':
                provider_info[provider["storage_hostname"]] = provider
            elif not exclude_in_error:
                provider_info[provider["storage_hostname"]] = provider
            else:
                LOG.debug("Provider excluded...")
            LOG.debug("Provider-INFO: %s." % provider)
        if storage_hostname is not None:
            # Since the specified provider was not found, raise an exception
            if volume_id:
                error = str(_("The volume with id '%(vol_id)s' references a "
                              "storage provider (%(prov)s) in property "
                              "'%(prop)s' that is not currently registered.")
                            % dict(vol_id=volume_id, prov=storage_hostname,
                                   prop=CINDER_HOST_KEY))
            else:
                error = _("The requested provider name, '%s', is not "
                          "currently registered.") % storage_hostname
            msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
            raise stgex.IBMPowerVCStorageError(msg)

        return provider_info

    def get_storage_type_for_volume(self, context, volume_id):
        """
        Return the 'stg-type' value from the provider_info structure
        given by get_stg_provider_info(). Note that the 'stg-type' is
        a folding of the 'backend_type" property:
            'ssp'      --> 'cluster'
            all others --> 'fc'

        :param context: The authorization context for a cinder REST call.
        :param volume_id: ID for the volume to look up the owning provider
                          for.
        """
        try:
            provider = self.get_stg_provider_info(context, volume_id=volume_id,
                                                  use_cache=True)
        except stgex.IBMPowerVCStorageError:
            # This specific error can occur if the cache did not have the
            # provider, so we just call again without the cache option.
            provider = self.get_stg_provider_info(context, volume_id=volume_id)

        return provider['stg_type']

    def list_volume_types(self, context):
        """ List volumes types """

        voltype_list =\
            cinder.cinderclient(context).volume_types.list()
        if not voltype_list:
            return []
        else:
            return [_untranslate_volume_type_view(voltype) for
                    voltype in voltype_list]


class PowerVCFabricAPI(cinder.API):
    """ Extend base cinder API with PowerVC wrapper for fabric calls"""
    def __init__(self):
        """ Constructor for the PowerVC volume API extension"""
        super(PowerVCFabricAPI, self).__init__()

    def get_fabrics(self, context):
        """
        Retrieve the list of fabric details from cinder. Return a
        dictionary keyed by fabric name.
        Example return:
            {
              'A': {
                       "fabric_name": "A",
                       "fabric_display_name": "fab184",
                       "access_ip": "9.114.181.184",
                       "user_id": "admin"
                   },
              'B': {
                       "fabric_name": "B",
                       "fabric_display_name": "fab185",
                       "access_ip": "9.114.181.185",
                       "user_id": "admin"
                   }
            {
        """
        fabrics = []
        cinder_c = cinder.cinderclient(context)
        try:
            urlpath = "/san-fabrics/detail"
            info = cinder_c.client.get(urlpath)
        except Exception as e:
            LOG.exception(e)
            msg = _("Unable to get fabrics from cinder service: "
                    "%s") % (e.__class__.__name__ + " - " + _('%s') % e)
            LOG.warn(msg)
            return fabrics
        # info will be returned as a tuple and we expect the second element
        # to contain the fabrics dictionary.
        if (len(info) != 2) or (not "fabrics" in info[1]):
            LOG.warn(str(info))
            msg = _("Unable to get fabrics from cinder service. Unexpected "
                    "data: %s") % str(info[0])
            LOG.warn(msg)
        LOG.debug("GET cinder '%s' returns: %s" % (urlpath, str(info)))

        name_to_fab = {}
        for fabric in info[1]['fabrics']:
            name_to_fab[fabric['fabric_name']] = fabric
        return name_to_fab

    def map_ports_to_fabrics(self, context, wwpn_list, give_message=False):
        """
        Given a list of FC Port WWPNs, this method calls to cinder to
        return a mapping of those wwpns to a registered fabric designation
        that each port is logged into. If a port is not found logged into
        a registered switch, its mapping will be None.
        Example:

            Input:
                [
                    "10000090FA2A5866",
                    "10000090FA2A8923",
                    "c0507606d56e03af"
                ]
            Returns:
                {
                    "10000090FA2A8923": "B",
                    "10000090FA2A5866": "A",
                    "c0507606d56e03af": None
                }
        """
        cinder_c = cinder.cinderclient(context)
        # Adjust the retries down to 0 so as not to waste time on nova client
        # side retries. The cinder brocade client has it's own set of
        # configurable retry logic. In addition, the host storage topology
        # reconciliation compute task will be retrying this on its periodic
        # interval.
        cinder_c.client.retries = 0
        try:
            urlpath = "/san-fabrics/map_ports"
            args = {'body': {'wwpn_list': wwpn_list}}
            info = cinder_c.client.get(urlpath, **args)
        except Exception as e:
            LOG.exception(e)
            msg = _("Unable to get port-to-fabric map from cinder service: "
                    "%s") % (e.__class__.__name__ + " - " + _('%s') % e)
            LOG.warn(msg)
            if give_message:
                return msg
            return None
        # info will be returned as a tuple and we expect the second element
        # to contain the fabrics dictionary.
        if (len(info) != 2) or (not "wwpn_mapping" in info[1]):
            LOG.warn(str(info))
            msg = _("Unable to get port-to-fabric map from cinder service. "
                    "Unexpected data: %s") % str(info)
            LOG.warn(msg)
            if give_message:
                return msg
            return None

        LOG.debug("GET cinder '%s' returns: %s" % (urlpath, str(info)))
        return info[1]['wwpn_mapping']
