#
# =================================================================
# =================================================================

import abc
import inspect

from sqlalchemy.orm.session import object_session

from nova.openstack.common import log as logging
LOG = logging.getLogger(__name__)

from nova.db.sqlalchemy import api as db_session


class ResourceStateMgmt(object):
    """
    TODO: 04 COMMENT
    """
    __metaclass__ = abc.ABCMeta

    '''BEGIN
    def __getattr__(self, name):
        super(ResourceStateMgmt, self).__getattr__(name)

    def __setattr__(self, name, value):
        super(ResourceStateMgmt, self).__setattr__(name, value)
    END'''

    @abc.abstractmethod
    def refresh(self, session=None):
        """
        TODO: 04 COMMENT
        """
        return

    @abc.abstractmethod
    def save(self, session=None):
        """
        TODO: 04 COMMENT
        """
        return

    @abc.abstractmethod
    def delete(self, session=None):
        """
        TODO: 04 COMMENT
        """
        return


class ResourceStateMgmt_dtoAPI(object):
    """
    TODO: Revisit SA issue in mapped DTOs inheriting from ABC ResourceStateMgmt
    """

    '''BEGIN
    def __getattr__(self, name):
        super(ResourceStateMgmt, self).__getattr__(name)

    def __setattr__(self, name, value):
        super(ResourceStateMgmt, self).__setattr__(name, value)
    END'''

    def refresh_with_context(self, context, session=None):
        """
        TODO: 04 COMMENT
        """
        raise NotImplementedError()

    def save_with_context(self, context, session=None):
        """
        TODO: 04 COMMENT
        """
        raise NotImplementedError()

    def delete_with_context(self, context, session=None):
        """
        TODO: 04 COMMENT
        """
        raise NotImplementedError()


class Resource(ResourceStateMgmt):
    """
    TODO: 04 COMMENT
    """
    __metaclass__ = abc.ABCMeta
    _context = None
    _dto = None

    def __init__(self, context, dto=None, **attr_kwargs):
        """
        Constructs a new instance of this resource (sub)type.
        Initially transient, this instance will use the given dto to
        manage its (potentially) persistent attributes and, along with
        the given context, any persistence operations.
        Any keyword settings in **attr_kwargs that match attributes named
        in either the dto (1st) or this instance itself (2nd) will be
        applied to set the initial state of this resource.  (And any
        keyword settings in **attr_kwargs that are NOT attributes of
        either the dto or this instance will be ignored.)
        """
        self._context = context
        self._dto = dto
        for key, value in attr_kwargs.items():
            if hasattr(self._dto, key) or hasattr(self, key):
                setattr(self, key, value)
            else:
                LOG.warn("Resource __init__ construction will skip setting of "
                         "UNRECOGNIZED '" + self.__class__.__name__
                         + "' ATTRIBUTE: '" + str(key) + "'= " + str(value))

    def __getattr__(self, name):
        '''BEGIN
        LOG.info('Resource.__getattr__: name = ' + name + ' -> '
                 + str(hasattr(self._dto, name)))
        END'''
        if hasattr(self._dto, name):
            return getattr(self._dto, name)
        else:
            return ResourceStateMgmt.__getattr__(self, name)

    def __setattr__(self, name, value):
        if hasattr(self._dto, name):
            setattr(self._dto, name, value)
        else:
            ResourceStateMgmt.__setattr__(self, name, value)

    def provide_session_for_this_resource(self, session):
        if session is None:
            session = object_session(self)
        if session is None:
            session = db_session.get_session()
            session.add(self)
        return session

    def save(self, session=None):
        """
        TODO: 04 COMMENT
        """
        if (hasattr(self, '_dto') and
                isinstance(self._dto, ResourceStateMgmt_dtoAPI)):
            return self._dto.save_with_context(self._context, session)
        else:
            LOG.debug('IN Resource.save: Invalid _dto is either missing or '
                      'not an instance of ResourceStateMgmt_dtoAPI')
            return None

    def delete(self, session=None):
        """
        TODO: 04 COMMENT
        """
        if (hasattr(self, '_dto') and
                isinstance(self._dto, ResourceStateMgmt_dtoAPI)):
            return self._dto.delete_with_context(self._context, session)
        else:
            LOG.debug('IN Resource.delete: Invalid _dto is either missing or '
                      'not an instance of ResourceStateMgmt_dtoAPI')
            return None


class ResourceFactory(object):
    """
    TODO: 04 COMMENT
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def construct_resource(self, context, resource_type, auto_save=False,
                           session=None, **kwargs):
        """
        Returns a new instance of the given resource type, constructed with
        the given keyword arguments, if any.  The instance will be transient
        (non-persistent) unless 'save' is called on it.
        """
        return

    @abc.abstractmethod
    def find_all_resources(self, context, resource_type, session=None,
                           where_equal_all_dict=None):
        """
        TODO: 6 COMMENT
        """
        return


def import_module(module_name_string):
    """
    TODO: 6 COMMENT
    """
    module = __import__(module_name_string)
    components = module_name_string.split('.')
    for comp in components[1:]:
        module = getattr(module, comp)
    return module


class FactoryLocator(object):
    """
    This is a kind of "meta-factory" that is used to obtain instances of
    ResourceFactories.  Using a FactoryLocator allows code to be independent
    of ResourceFactory implementations, and allows a form of <it>dependency
    injection</it> for factories, which in turn applies to DOM resources.
    """
    _default_f_locator_instance = None
    _factory_type_to_impl_mapping = {}
    _default_factoryimpl_module_name = 'paxes_nova.db.sqlalchemy.api'

    @classmethod
    def construct(cls):
        """
        TODO: 04 COMMENT
        """
        return FactoryLocator()

    @classmethod
    def get_default_locator(cls):
        """
        TODO: 04 COMMENT
        """
        if cls._default_f_locator_instance is None:
            cls._default_f_locator_instance = cls.construct()
        return cls._default_f_locator_instance

    def _find_factory_class_in_module(self, resourcefactory_type, module=None):
        """
        Searches the module of the given resourcefactory_type and
        returns the first subclass of ResourceFactory found, if any; else None
        """
        if module is None:
            module = import_module(self._default_factoryimpl_module_name)
        '''BEGIN
        LOG.info(self.__class__.__name__ + '.' + inspect.stack()[0][3]
                 + ': module = ' + str(module))
        END'''
        for name, obj in inspect.getmembers(module):
            # TODO: 03 Should check that the factory subclass in not an ABC?
            if (inspect.isclass(obj) and
                issubclass(obj, resourcefactory_type) and
                    not inspect.isabstract(obj)):
                return obj
        return None

    def locate_factory(self, resourcefactory_type):
        """
        TODO: 04 COMMENT
        """
        if not issubclass(resourcefactory_type, ResourceFactory):
            # TODO 04 Determine this error: exception? type? RAS?
            raise TypeError('Expected a subclass of ResourceFactory for: '
                            'resourcefactory_type = '
                            + str(resourcefactory_type))
        factory_class = self._find_factory_class_in_module(
            resourcefactory_type)
        if factory_class is None:
            LOG.warn('Failed to find implementation class for factory type = '
                     + str(resourcefactory_type))
        return factory_class()


class FactoryTypeImplMap(object):
    """
    TODO: 04 COMMENT
    """
    # TODO: 01 Make this a fully fledge dictionary?
    pass
