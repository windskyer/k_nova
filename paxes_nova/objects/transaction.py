# =================================================================
# =================================================================
import abc


class Transaction(object):
    """
    Supertype for a user-managed txn life-cycle: begin-commit-rollback.

    Instances can be used in Python 'with' statements.
    """
    __metaclass__ = abc.ABCMeta

    ###############################################
    ### Defining Resource Transaction Interface ###
    ###############################################
    @abc.abstractmethod
    def begin(self, *args, **kwargs):
        """Begins the Transaction that was previously constructed"""
        return self

    @abc.abstractmethod
    def commit(self, *args, **kwargs):
        """Commits any pending resource changes as part of the Transaction"""
        pass

    @abc.abstractmethod
    def rollback(self, *args, **kwargs):
        """Roll-back any pending resource changes as part of the Transaction"""
        pass

    @abc.abstractmethod
    def close(self, *args, **kwargs):
        """Closes any open connections/sessions as part of this Transaction"""
        if self.session is not None:
            self.close()

    ###############################################
    ####  Internal Transaction Helper Methods  ####
    ###############################################
    @abc.abstractmethod
    def __enter__(self):
        """Provided entry method for support of the 'with' statement"""
        return self

    @abc.abstractmethod
    def __exit__(self, a_type, value, traceback):
        """Provided exit method for support of the 'with' statement"""
        pass
