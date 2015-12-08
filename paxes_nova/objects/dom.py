#
# =================================================================
# =================================================================
import abc
import sys
from datetime import datetime
import nova.exception
from nova.objects import base
from nova.objects import fields
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging

from paxes_nova import _
_RESOURCE_FACTORY_MAP = dict()
LOG = logging.getLogger(__name__)

from paxes_nova.objects.transaction import Transaction


######################################################
###########  Base DOM Resource Definition  ###########
######################################################
class Resource(base.NovaObject):
    """The Resource class provides the Base DOM Definition/Implementation"""

    #Defines the list of Attributes Names to Hydrate into this Resource
    fields = dict(resource_created=fields.BooleanField(),
                  created_at=fields.DateTimeField(nullable=True),
                  updated_at=fields.DateTimeField(nullable=True),
                  factory_class_name=fields.StringField(nullable=True))

    #Defines the list of Attributes Names that aren't Hydrated for the Resource
    obj_extra_fields = []

    # Names of fields to be INCLUDED in attribute hydration
    _public_resource_field_keys = ['created_at', 'updated_at']

    def __init__(self, context=None, factory_class=None, transaction=None,
                 **kwargs):
        "Constructs an instance of the given DOM Resource Class"
        super(Resource, self).__init__()
        self._context = context
        self._transaction = transaction
        #If they gave us the DTO object, then just use that as is
        attribs = kwargs['dto'] if 'dto' in kwargs else kwargs
        #Get the Factory Class Name to keep track of across the serialization
        if factory_class is not None:
            self.factory_class_name =\
                factory_class.__module__ + '.' + factory_class.__name__
        self.resource_created = 'dto' in kwargs
        #Hydrate all of the attributes on the object
        self._initialize_resource_attributes()
        self._hydrate_all_resource_attributes(attribs, factory_class is None)

    ###############################################
    ######  Defining DOM Resource Interface  ######
    ###############################################
    def get_transaction(self, auto_construct=False):
        """
        Returns a transaction to begin or continue using for this resource.

        :auto_construct:
        """
        if self._transaction is None and auto_construct:
            self._transaction = self._get_resource_factory().\
                get_resource_transaction()
        return self._transaction

    def refresh(self, context=None, transaction=None):
        """Refreshes the latest state of the Resource attributes"""
        self._update_transaction(context, transaction)
        #Get the Identifier Attribute to know which Resource to Refresh
        id_attr = self._get_resource_id_attribute()
        resource_id = self._get_resource_identifer()
        filters = {id_attr: resource_id}
        #Delegate to the Factory to Retrieve the Refreshed Resource
        new_resource = self._get_resource_factory().get_resource(
            self.__class__, self._context, filters, self._transaction)
        #Loop through the retrieved resource, updating the current resource
        for field in self._get_public_resource_fields():
            if hasattr(new_resource, base.get_attrname(field)):
                setattr(self, field, getattr(new_resource, field))
        #Resets any attributes that have been cached on this resource
        self._reset_cached_attributes()
        self.obj_reset_changes()

    def save(self, context=None, transaction=None):
        """Creates/Updates the Attributes on the given Resource"""
        self._update_transaction(context, transaction)
        #If the Resource already exists then we will just Update it
        if self.resource_created:
            self._update_resource(self._context, self._transaction)
        #If the Resource doesn't exists then we need to Create it
        else:
            self._create_resource(self._context, self._transaction)
        #Since we just created/updated it, we know that it currently exists
        self.obj_reset_changes()
        setattr(self, 'resource_created', True)

    def delete(self, context=None, transaction=None):
        """Deletes the given Resource from the Persistence"""
        self._update_transaction(context, transaction)
        #Get the Identifier Attribute to know which Resource to Delete
        resource_id = self._get_resource_identifer()
        #Delegate to the Factory to do the actual Deletion of the Resource
        self._get_resource_factory().delete_resource(
            self.__class__, self._context, resource_id, self._transaction)
        #Since it was deleted, mark it as if it doesn't exist any more
        self._reset_cached_attributes()
        self.obj_reset_changes()
        setattr(self, 'resource_created', False)

    ###############################################
    ### Defining DOM Resource Abstract Methods ####
    ###############################################
    @classmethod
    def get_id_attr_name(cls):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'id'

    # TODO 5 Remove this instance-level version?
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return self.get_id_attr_name()

    def _hydrate_resource_attribute(self, attributes, key, value):
        """Hydrates a given Attribute onto this Resource Instance"""
        # TODO: 5 'attributes' is not used.  Is it needed?
        setattr(self, key, value)

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        pass

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        pass

    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        raise ResourceOperationNotSupported(operation='create',
                                            class_name=self.__class__.__name__)

    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        raise ResourceOperationNotSupported(operation='update',
                                            class_name=self.__class__.__name__)

    ###############################################
    #####  Shared DOM Resource Helper Methods  ####
    ###############################################
    def _get_resource_factory(self):
        """Helper method to return the Factory class for this Resource"""
        index = self.factory_class_name.rfind('.')
        module_name = self.factory_class_name[:index]
        class_name = self.factory_class_name[index + 1:]
        factory_class = getattr(sys.modules[module_name], class_name)
        return ResourceFactoryLocator.get_factory(factory_class)

    def _get_resource_identifer(self):
        """Helper Method to get the Resource Identifier for the Resource"""
        id_attr = self._get_resource_id_attribute()
        #We can't continue on if the Identifier Attribute doesn't exist
        if not hasattr(self, id_attr) or getattr(self, id_attr) is None:
            raise ResourceIsUnidentifiable(id_attr=id_attr,
                                           class_name=self.__class__.__name__)
        return getattr(self, id_attr)

    def _get_resource_values(self, changes_only=False):
        """Helper Method to Parse the Attributes to Write to Persistence"""
        values = {}
        changes = self.obj_what_changed()
        for field in self._get_public_resource_fields():
            if not changes_only or field in changes:
                if hasattr(self, base.get_attrname(field)):
                    values[field] = self[field]
                    #Need to convert all date times to strings for the db
                    if isinstance(values[field], datetime):
                        format = '%Y-%m-%dT%H:%M:%S.%f'
                        values[field] = values[field].strftime(format)
        return values

    def _hydrate_all_resource_attributes(self, attribs, skip_initialize=True):
        """Helper Method to Hydrate the Attributes for the Resource"""
        #Loop through each of the defined fields, hydrating that attribute
        for field in self._get_public_resource_fields():
            value = attribs.get(field)
            if not skip_initialize or value is not None:
                self._hydrate_resource_attribute(attribs, field, value)
        self.obj_reset_changes()

    def _get_public_resource_fields(self):
        """Helper Method to return only the Public Fields"""
        public_fields = list(Resource._public_resource_field_keys)
        for key in self.fields.keys():
            if key not in Resource.fields:
                public_fields.append(key)
        return public_fields

    def _update_transaction(self, context=None, transaction=None):
        """Helper Method to update the Context/Transaction for the Resource"""
        if context is not None:
            self._context = context
        if transaction is not None:
            self._transaction = transaction


######################################################
###### Resource Factory Locator Implementation #######
######################################################
class ResourceFactoryLocator(object):
    """The ResourceFactoryLocator class provides the manager of Factories"""

    @classmethod
    def get_factory(cls, factory_class=None):
        """Locates the Factory for the given Factory Class/Calling Class"""
        #If no Factory Class was specified, then assume they want
        #the Factory for the class they are calling the method on
        if factory_class is None:
            factory_class = cls
        full_name = factory_class.__module__ + '.' + factory_class.__name__
        factory = _RESOURCE_FACTORY_MAP.get(full_name)
        #Throw an exception if we didn't find a matching factory
        if factory is None:
            raise ResourceFactoryNotFound(class_name=full_name)
        return factory

    @classmethod
    def register_factory(cls, factory, override=False):
        """Registers a Factory as the Implementation for Factory Classes"""
        classes = [factory.__class__] + factory._get_factory_classes()
        #Loop through all of the Classes for the Factory, registering them
        for clazz in classes:
            full_name = clazz.__module__ + '.' + clazz.__name__
            exist_factory = _RESOURCE_FACTORY_MAP.get(full_name)
            #Only register if there isn't one, or they want to override
            if override or exist_factory is None:
                _RESOURCE_FACTORY_MAP[full_name] = factory


######################################################
#######  Resource Transaction Implementation  ########
######################################################
class ResourceTransaction(Transaction):
    """User-managed txn for a set of Resources.

    It may be a composite of subtxns for DB, remote systems, etc.
    """
    __metaclass__ = abc.ABCMeta


class ResourceTransaction_dummy(ResourceTransaction):
    """An inert impl'n of ResourceTransaction, used for "indirection" runtimes
    """
    pass


######################################################
#######  Base DOM Resource Factory Definition  #######
######################################################
class ResourceFactory(base.NovaObject, ResourceFactoryLocator):
    """The ResourceFactory class provides the Base DOM Factory definition"""

    DEFAULT_RESOURCE_TRANSACTION_CLASS = None

    def __init__(self):
        "Constructs an instance of the given DOM Resource Factory Class"
        super(ResourceFactory, self).__init__()

    ###############################################
    #### Defining DOM Resource FactoryInterface ###
    ###############################################
    def get_resource_transaction_class(self):
        if ResourceFactory.DEFAULT_RESOURCE_TRANSACTION_CLASS is None:
            raise ResourceOperationNotSupported(
                operation='get_resource_transaction_class',
                class_name=self.__class__.__name__)
        else:
            return ResourceFactory.DEFAULT_RESOURCE_TRANSACTION_CLASS

    def get_resource_transaction_indirection_class(self):
        return ResourceTransaction_dummy

    def get_resource_transaction(self):
        """Returns a new instance of ResourceTransaction"""
        #If this is will call to the Conductor, no need to get a Session
        if base.NovaObjectMetaclass.indirection_api is None:
            return self.get_resource_transaction_class()()
        return None

    def construct_resource(self, resource_class, context,
                           auto_create=False, transaction=None, **kwargs):
        """Constructs (and optionally creates) the Resource specified"""
        actual_class = self._get_actual_resource_class(resource_class)
        resource_inst = actual_class(
            context, self.__class__, transaction, **kwargs)
        #If they asked us to auto-create it, call save to cause it to create
        if auto_create:
            resource_inst.save()
        return resource_inst

    def get_resource(self, resource_class, context,
                     filters=None, transaction=None):
        """Queries exactly one Resource matching the Filters specified"""
        resource = self.find_resource(
            resource_class, context, filters, transaction)
        #If a matching Resource wasn't found, then throw an exception
        if resource is None:
            raise ResourceNotFound(filters=str(filters),
                                   class_name=resource_class.__name__)
        return resource

    def find_resource(self, resource_class, context,
                      filters=None, transaction=None):
        """Queries at most one Resource matching the Filters specified"""
        resources = self.find_all_resources(
            resource_class, context, filters, transaction)
        #If more than one Resource matched, then throw an exception
        if len(resources) > 1:
            raise MultipleResourcesFound(filters=str(filters),
                                         class_name=resource_class.__name__)
        return resources[0] if len(resources) > 0 else None

    def find_all_resources(self, resource_class, context,
                           filters=None, transaction=None):
        """Queries the Resources based on the map of Filters specified"""
        actual_class = self._get_actual_resource_class(resource_class)
        actual_class = actual_class.__module__ + '.' + actual_class.__name__
        return self._find_all_resources(
            context, actual_class, filters, transaction)

    def delete_resource(self, resource_class, context,
                        resource_id, transaction=None):
        """Deletes the Resource based on the Identifier specified"""
        self.delete_multiple_resources(resource_class, context,
                                       [resource_id], transaction)

    def delete_multiple_resources(self, resource_class, context,
                                  resource_ids, transaction=None):
        """Deletes the Resources based on the list of ID's specified"""
        actual_class = self._get_actual_resource_class(resource_class)
        actual_class = actual_class.__module__ + '.' + actual_class.__name__
        #Ask the Factory Implementation to actually delete the Resources
        self._delete_multiple_resources(context, actual_class,
                                        resource_ids, transaction)

    ###############################################
    # Defining Resource Factory Abstract Methods ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        raise NotImplementedError()

    def _get_factory_classes(self):
        """Returns a List of Factory Classes that this Factory provides"""
        return [self.__class__]

    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        raise ResourceOperationNotSupported(operation='query',
                                            class_name=resource_class.__name__)

    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        raise ResourceOperationNotSupported(operation='delete',
                                            class_name=resource_class.__name__)

    ###############################################
    #### Shared Resource Factory Helper Methods ###
    ###############################################
    @staticmethod
    def _load_resource_class(resource_class):
        """Helper method to load the Resource Class if a string was provided"""
        #If this is the String class Name, then we need to load the class
        if ((isinstance(resource_class, str) or
             isinstance(resource_class, unicode))):
            module_name = resource_class[:resource_class.rfind('.')]
            class_name = resource_class[resource_class.rfind('.') + 1:]
            return getattr(sys.modules[module_name], class_name)
        return resource_class

    def _get_actual_resource_class(self, resource_class):
        """Helper method to map the provided class to the implementing class"""
        resource_classes = self._get_resource_classes()
        #If this is a list, just see if the class exists in the list
        if isinstance(resource_classes, list):
            if resource_class in resource_classes:
                return resource_class
        #If they provided a map, then we need to return the actual class
        if isinstance(resource_classes, dict):
            if resource_class in resource_classes.keys():
                return resource_classes[resource_class]
            #In case we were provided the Implementation Class, check for it
            if resource_class in resource_classes.values():
                return resource_class
        #If it wasn't found, say that the given class isn't supported
        raise ResourceClassNotSupported(class_name=resource_class.__name__)

    def _construct_resources(self, resource_class, context,
                             db_resources, transaction=None):
        """Helper method to construct a list of Resources from the DB"""
        resource_list = []
        resource_class = self._load_resource_class(resource_class)
        actual_class = self._get_actual_resource_class(resource_class)
        #Loop through each of the DB Resources, constructing an instance
        for db_resource in db_resources:
            #Construct an Instance of the Resource Class were were given
            resource_inst = actual_class(
                context, self.__class__, transaction, dto=db_resource)
            resource_list.append(resource_inst)
        return resource_list


######################################################
########  DOM Resource Exception Definitions  ########
######################################################
class ResourceFactoryNotFound(nova.exception.NovaException):
    msg_fmt = _("No Resource Factory was registered for %(class_name)s.")


class ResourceNotFound(nova.exception.NovaException):
    msg_fmt = _("No %(class_name)s Resource found for filters: %(filters)s.")


class MultipleResourcesFound(nova.exception.NovaException):
    msg_fmt = _("More than 1 %(class_name)s Resources "
                "found for filters: %(filters)s.")


class ResourceIsUnidentifiable(nova.exception.NovaException):
    msg_fmt = _("Cannot access id attribute: '%(id_attr)s'"
                " for class: %(class_name)s.")


class ResourceClassNotSupported(nova.exception.NovaException):
    msg_fmt = _("The Resource Factory does not support %(class_name)s.")


class ResourceOperationNotSupported(nova.exception.NovaException):
    msg_fmt = _("The Resource Operation '%(operation)s' "
                "is not supported on %(class_name)s.")


######################################################
#########  DOM Resource Fields Definitions  ##########
######################################################
class AnyType(fields.FieldType):
    @staticmethod
    def coerce(obj, attr, value):
        return value


class MetaDataDict(fields.Dict):
    def coerce(self, obj, attr, value):
        if isinstance(value, list):
            value = dict([(itm['key'], itm['value']) for itm in value])
        return super(MetaDataDict, self).coerce(obj, attr, value)


class InfoCacheDict(fields.Dict):
    def coerce(self, obj, attr, value):
        if not isinstance(value, dict):
            uuid = value.get('instance_uuid')
            info = jsonutils.loads(value['network_info'])
            value = dict(instance_uuid=uuid, network_info=info)
        return super(InfoCacheDict, self).coerce(obj, attr, value)


class DictOfAnyTypeField(fields.AutoTypedField):
    AUTO_TYPE = fields.Dict(AnyType(), nullable=True)


class MetaDataDictField(fields.AutoTypedField):
    AUTO_TYPE = MetaDataDict(fields.String(), nullable=True)


class InfoCacheDictField(fields.AutoTypedField):
    AUTO_TYPE = InfoCacheDict(AnyType(), nullable=True)


###################################################
###########   Internal Model Utilities   ##########
###################################################
class ChangeTrackedList(list):
    """A special implementation of lists to be used for DOM associations.

    This list implementation intercepts list-modifying operations and
    propagates such change-events to the owning DOM instance by resetting
    the list property.  This is intended to signal dirty-tracking in the
    DOM object that owns the list.

    NOTE: This implementation DOES NOT handle any association updates in the
    reverse direction, i.e., from elements to their list-owners, old or new.
    """

    def __init__(self, dom_list_setter, initial_iterable=None):
        if initial_iterable is None:
            initial_iterable = []
        super(ChangeTrackedList, self).__init__(initial_iterable)
        self.list_setter = dom_list_setter

    def extend(self, iterable):
        super(ChangeTrackedList, self).extend(iterable)
        self.list_setter(self)

    def append(self, element):
        """Note that modifying the list does NOT automatically align any
        reverse references (cf. SQLAlchemy "backrefs") from the element to
        list owner(s), old or new.
        """
        super(ChangeTrackedList, self).append(element)
        self.list_setter(self)

    def remove(self, element):
        """Note that modifying the list does NOT automatically align any
        reverse references (cf. SQLAlchemy "backrefs") from the element to
        list owner(s), old or new.
        """
        super(ChangeTrackedList, self).remove(element)
        self.list_setter(self)

    def pop(self, element):
        """Note that modifying the list does NOT automatically align any
        reverse references (cf. SQLAlchemy "backrefs") from the element to
        list owner(s), old or new.
        """
        ret = super(ChangeTrackedList, self).pop(element)
        self.list_setter(self)
        return ret

    def insert(self, index, element):
        """Note that modifying the list does NOT automatically align any
        reverse references (cf. SQLAlchemy "backrefs") from the element to
        list owner(s), old or new.
        """
        super(ChangeTrackedList, self).insert(index, element)
        self.list_setter(self)

    def reverse(self):
        super(ChangeTrackedList, self).reverse()
        self.list_setter(self)

    def sort(self, compare=None, key=None, reverse=False):
        super(ChangeTrackedList, self).sort(compare, key, reverse)
        self.list_setter(self)
