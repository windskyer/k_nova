# =================================================================
# =================================================================

###################################################
#######    Cascading from Soft-Deletes      #######
###################################################
"""
Whenever PowerVC creates a new object-type whose instances depend on "parent"
objects of existing OpenStack (Nova) types, it may be necessary to cascade
soft-deletes from such parents to the dependent objects of the new type.
This module provides a mechanism for intercepting Nova soft-deletes and
forwarding the delete operation to the dependent objects of registered types.
"""
from datetime import datetime, timedelta
from sqlalchemy.schema import Table
from sqlalchemy.sql.expression import Join

import nova.context as nova_context

import nova.openstack.common.db.sqlalchemy.session as nova_session
import nova.openstack.common.db.sqlalchemy.models as nova_common_models

import nova.db.sqlalchemy.api as nova_db_sa_api


class DependentType(object):
    """This describes a meta-model dependency between two SqlA-mapped classes.

    A DependentType is a kind of relationship object between two SA-mapped
    (i.e., table-backed) classes, the parent and a dependent, where the
    dependent's table has some kind of foreign-key-like relationship to the
    parent's table.  This dependency relationship information supports the
    cascading of OpenStack (OS) soft-deletes from parents to their dependents
    for those cases where the dependents are not already managed by the OS
    logic.  The parent-type's soft-delete will cascade as either a soft
    (default) or a hard delete to each dependent-type.
    """
    def __init__(self, dependent_class, fkey_attr_name, parent_class,
                 pkey_attr_name='id', is_deletion_soft=True):
        self.dep_class = dependent_class
        self.fkey_attr = fkey_attr_name
        self.parent_class = parent_class
        self.pkey_attr = pkey_attr_name
        self.is_deletion_soft = is_deletion_soft


class DepTypeMgmt(object):
    """Provide one object to manage the relationships of PVC's dependent types.

    When PowerVC (PVC) creates classes/types whose objects depend on
    OpenStack (OS) objects, there needs to be a way to cascade OS soft-deletes
    from an OS object to its PVC dependents.  This class and its related
    method-wrapping and before/after functions provide a mechanism for
    tracking the dependencies and triggering the cascading of soft-deletes
    from the OS parent objects to the PVC dependents.

    NOTE: A limitation of the current implementation is that it does not
    detect or handle cyclic dependencies.  A soft-delete involving a cycle of
    dependents might result in an "infinite" recursion of soft-deletes.
    """
    # Time window (in seconds) for selecting recently soft-deleted parents
    DELETEDAT_SECONDS_DELTA = 10
    KEY_ORIGINAL_RETURN = 'return_value_of_wrapped_original'

    _is_initialized_pvc_models = False
    _dependency_mgr = None

    @classmethod
    def instance(cls):
        """Returns the current, presumably unique, instance of DepTypeMgmt."""
        if cls._dependency_mgr is None:
            cls.set_instance(DepTypeMgmt())
        return cls._dependency_mgr

    @classmethod
    def set_instance(cls, dependency_mgr):
        """Sets the current, presumably unique, instance of DepTypeMgmt."""
        cls._dependency_mgr = dependency_mgr

    def __init__(self):
        if not DepTypeMgmt._is_initialized_pvc_models:
            DepTypeMgmt._is_initialized_pvc_models = True
            # Replace Nova's 2 soft_deletes with our cascade-wrapped ones
            nova_session.Query.soft_delete = wrap_method(
                nova_session.Query.soft_delete,
                before_fn=before_query_sdel,
                after_fn=after_query_sdel)
            nova_common_models.SoftDeleteMixin.soft_delete = wrap_method(
                nova_common_models.SoftDeleteMixin.soft_delete,
                before_fn=before_mixin_sdel,
                after_fn=after_mixin_sdel)
            self._table_to_deps_dict = {}

    def registerDepTypeForDeleteCascades(self, dependent_class, fkey_attr_name,
                                         parent_class, pkey_attr_name='id',
                                         is_deletion_soft=True):
        """Record a class dependency for cascading from parent's soft-deletes.

        This documents a dependent class's foreign-key-like relationship to
        a parent class.  This supports the cascading of a parent object's
        soft-delete to also delete its dependent objects.

        :param dependent_class: A SqlA-mapped class.  No other arguments are
        needed if this has exactly one attribute mapped to a SA ForeignKey.

        :param fkey_attr_name: (string) Name of the dependent's attribute
        that has the parent's foreign-key value (typically an 'id' value).
        This arg is required if the ForeignKey to the parent cannot be
        uniquely determined from dependent and parent if given.

        :param parent_class: A SqlA-mapped class.  It's table-name will be
        used as the key to this dependency.

        :param pkey_attr_name: (string) Name of the parent's primary-key
        attribute (typically 'id'), which provides the dependent's
        foreign-key value.  This is required if the attribute is not 'id'.

        :param is_deletion_soft: if True, a parent's soft-delete will be
        cascaded as a soft-delete to the dependents; if False, the dependents
        will be hard-deleted (cascaded from their parent's soft-delete).

        :returns: the list of DependentTypes for the given parent, which
        will cover at least the given dependent, and possibly others
        """
        # Find the parent table and save its name to use as a key
        parent_tablename = parent_class.__tablename__
        # If needed, create a new list of DependentTypes for this parent
        if parent_tablename not in self._table_to_deps_dict:
            self._table_to_deps_dict[parent_tablename] = []
        dep_type = DependentType(dependent_class, fkey_attr_name,
                                 parent_class, pkey_attr_name,
                                 is_deletion_soft)
        # If needed, create a new list of DependentTypes for this parent
        if parent_tablename not in self._table_to_deps_dict:
            self._table_to_deps_dict[parent_tablename] = []
        # If needed, add this DependentType for this parent's list
        if dep_type not in self._table_to_deps_dict[parent_tablename]:
            self._table_to_deps_dict[parent_tablename].append(dep_type)
        return self._table_to_deps_dict[parent_tablename]

    def find_dependent_types(self, parent_tablename):
        # Returns list of dependent types based on the given (parent's) table
        return self._table_to_deps_dict.get(parent_tablename, [])


def wrap_method(original_method, before_fn=None, after_fn=None):
    """Returns a compound method that consists of the original one sandwiched
    between the before and after functions.

    This function can be used to "monkey patch" a method in existing code by
    replacing it with a compound version of itself.
    The parameters for all methods & functions are: '(self, *args, **kwargs)'.
    The before_fn, if defined, returns args & kwargs that are passed to the
    original_method & the after_fn if defined.
    The after_fn receives the return-value of the original method via kwargs:
        "kwargs[KEY_ORIGINAL_RETURN] = return_value_original"

    wrapped_method returns: if there is no after_fn, then it returns the value
    of the original method; else the return is that of the after_fn, which
    can choose the original-method's return value or something different.
    """
    def wrapped_method(self, *args, **kwargs):
        if before_fn is None:
            args_kwargs_tuple = (args, kwargs)
        else:
            args_kwargs_tuple = before_fn(self, *args, **kwargs)
        return_value_original = original_method(self, *(args_kwargs_tuple[0]),
                                                **(args_kwargs_tuple[1]))
        args_kwargs_tuple[1][DepTypeMgmt.KEY_ORIGINAL_RETURN] = \
            return_value_original
        if after_fn is None:
            return return_value_original
        else:
            return after_fn(self, *(args_kwargs_tuple[0]),
                            **(args_kwargs_tuple[1]))
    return wrapped_method


def before_query_sdel(sdel_query, *args, **kwargs):
    """Called immediately before Nova's session.Query.soft_delete,
    it prepares the expected kwargs (as in the original method).
    """
    # Ensure the arguments expected by Query.soft_delete
    if 'synchronize_session' not in kwargs:
        kwargs['synchronize_session'] = 'evaluate'
    return (args, kwargs)


def _revise_query_for_deleted_parents(soft_delete_query):
    """Remove the 'deleted == 0' clause if it exists in the soft-delete query.

    This assumes that the query was originally used to delete a set of
    parent objects.  Removing the 'deleted' clause allows it to be reused and
    still include the originally deleted parents.

    :returns: nothing
    """
    if hasattr(soft_delete_query.whereclause, 'clauses'):
        where_clause_list = soft_delete_query.whereclause.clauses
    else:
        where_clause_list = [soft_delete_query.whereclause]
    for clause in where_clause_list:
        if (hasattr(clause, 'left') and
                clause.left.name == 'deleted' and
                clause.right.value == 0):
            # Remove this 'undeleted' clause to re-select (now deleted) parents
            soft_delete_query.whereclause.clauses.remove(clause)
            break


def _delete_dependents_for_all_parents(parent_list, dep_type_list, kwargs):
    """Delete all dependents of each parent, returning the # deleted.
    """
    deleted_count = 0
    if len(parent_list) * len(dep_type_list) == 0:
        return deleted_count
    # Process arguments: Context
    context = kwargs.get('context', None)
    if context is None:
        context = nova_context.get_admin_context()
    # For each dependent-type and each parent, delete all the dependents
    for dep_type in dep_type_list:
        query = nova_db_sa_api.model_query(context, dep_type.dep_class,
                                           **kwargs)
        filters_dict = {'deleted': 0}
        # For each parent object, delete all of it dependent objects
        for parent in parent_list:
            filters_dict[dep_type.fkey_attr] = getattr(parent,
                                                       dep_type.pkey_attr)
            if dep_type.is_deletion_soft:
                deleted_count += query.filter_by(**filters_dict).soft_delete()
            else:
                deleted_count += query.filter_by(**filters_dict).delete()
            filters_dict.pop(dep_type.fkey_attr)
    return deleted_count


def after_query_sdel(sdel_query, *args, **kwargs):
    """Called immediately after Nova's session.Query.soft_delete,
    this deletes all dependents of queried 'parents', returning the # deleted.

    Given a soft-delete query that was presumably used to delete a set of
    parent objects (rows), try to determine the set of parents and then
    delete all of their dependents.  Firstly, find all of the dependent types
    that were registered to PVC's DepTypeMgmt; this is based on the parents'
    table, found in the query's 'FROM'.  Secondly, reuse a version of the
    parents' original soft-delete query to find all of the soft-deleted
    parents.  Finally, delete all dependents of those parents.
    """
    # From the sd-query, find the parent-type and its dependent-types (if any)
    # Assume that the parent-type is the query's 1st 'FROM' table
    parent_table = sdel_query.statement.froms[0]
    #This might include a join, so part out the table name from that join
    if isinstance(parent_table, Join):
        if isinstance(parent_table.left, Table):
            parent_table = parent_table.left
        elif isinstance(parent_table.right, Table):
            parent_table = parent_table.right
    parent_tablename = parent_table.name
    dep_type_list = DepTypeMgmt.instance().find_dependent_types(
        parent_tablename)
    if len(dep_type_list) > 0:
        _revise_query_for_deleted_parents(sdel_query)
        # Find all parents that were recently soft-deleted
        recent_dt = (datetime.utcnow()
                     - timedelta(seconds=DepTypeMgmt.DELETEDAT_SECONDS_DELTA))
        sdel_query = sdel_query.filter(getattr(dep_type_list[0].parent_class,
                                               'deleted_at') >= str(recent_dt))
        parent_list = sdel_query.all()
        _delete_dependents_for_all_parents(parent_list, dep_type_list, kwargs)
    return kwargs[DepTypeMgmt.KEY_ORIGINAL_RETURN]


def before_mixin_sdel(sdel_mixin, *args, **kwargs):
    """Called immediately before Nova's models.SoftDeleteMixin.soft_delete,
    it prepares the expected kwargs (as in the original method).
    """
    # Ensure the arguments expected by SoftDeleteMixin.soft_delete
    if 'session' not in kwargs:
        kwargs['session'] = None
    return (args, kwargs)


def after_mixin_sdel(sdel_mixin, *args, **kwargs):
    """Called immediately after Nova's models.SoftDeleteMixin.soft_delete,
    this deletes all the parent-mixin's dependents, returning the # deleted.
    """
    # Process arguments: Context
    context = kwargs.get('context', None)
    if context is None:
        context = nova_context.get_admin_context()
    # Find the dependent-types for the given Mixin parent object
    dep_type_list = DepTypeMgmt.instance().find_dependent_types(
        sdel_mixin.__tablename__)
    _delete_dependents_for_all_parents([sdel_mixin], dep_type_list, kwargs)
    return kwargs[DepTypeMgmt.KEY_ORIGINAL_RETURN]
