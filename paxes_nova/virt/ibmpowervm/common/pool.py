# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

import paramiko

from nova.openstack.common import log as logging

from paxes_nova.virt.ibmpowervm.common.connection import Connection
from paxes_nova.virt.ibmpowervm.common.connection import SSHConnection

from threading import Condition, Thread

from paxes_nova import _

LOG = logging.getLogger(__name__)


class ConnectionPool(object):

    '''
        A connection pool object which facilitates a use to pool the
        connection.
        By default it uses a generic connection object which should be
        extended to give custom connection related functionality.

        The class gives an interface with some default implementation
        and functionality. User of the API can extend the feature as
        per there need.

        @summary:

        Details of the attributes:

            user_name :
            The user name required for the default connection object.
            Default value is a blank string for the dummy connection
            object.

            password :
            The password for the user for the connection object.
            Default value is a blank string for the dummy connection
            object.

            host :
            Host IP or hostname for which connection needs to be generated.
            Default value is a blank string for the dummy connection
            object.

            port:
            Port to which connection will listen or connect.
            Default value is 0 for the dummy connection
            object.

            min_pool_size :
            Initial keep alive connections in the pool.
            Default value for this attribute is 5

            max_pool_size :
            Maximum size of the alive connection in the pool.
            Default value for this attribute is 10.

            wait_if_busy: (True or False)
            True: Indicates if there are not connection wait for
                  connection to be released.
            False: Indicates if there are no idle connection return
                    a None object immediately.

            wait_interval:
            Interval in seconds to which a thread will wait to re-check
            the connection availability. The wait_interval variable is
            used when we want to make sure that we don't wait for connection
            greater than the time we want to wait.
            Once the thread is awakened form the wait it will release the
            connection and then check whether the connection is available
            or not. If the connection is not available it will return a
            None object
            If we want to have the connection always from the pool, we should
            configure the pool with a negative wait interval where thread
            will wait for the connection till its available and notified.

            Note:

            In a negative wait interval time user of the pool should always
            release the connection when they are finished with the operations
            on the connection.

    '''
    _active_connection = []
    _idle_connection = []
    _condition = Condition()

    def __init__(self, user_name='', password='',
                 host='', port=0, min_pool_size=5,
                 max_pool_size=10, wait_if_busy=False,
                 wait_interval=600):
        '''
            Constructor to initialize connection pool object.
            All the attributes are defined in the summary of the
            connection pool class.
        '''

        # minimum pool size
        self._min_pool_size = min_pool_size
        # maximum pool size
        self._max_pool_size = max_pool_size
        # flag to indicate whether to wait or not if connections
        # are not available
        self._wait_if_busy = wait_if_busy
        # interval in seconds for waiting.
        self._wait_interval = wait_interval
        # user name for the connection object
        self._user_name = user_name
        # password for the connection object
        self._password = password
        # host for which we need to make connection
        self._host = host
        # port to connect on the host
        self._port = port
        # initialize the initial connection pool
        self._initialize()

    def _initialize(self):
        '''
            Private method, should not be called from outside.
            Used by pool constructor to initialize the initial
            connections inside the pool
        '''
        LOG.debug("Initializing the connection pool")
        # check if the min size passed is greater the maxe size
        if self._min_pool_size > self._max_pool_size:
            # if min > max make min = max
            self._min_pool_size = self._max_pool_size
        # initialize the connection pool to the minimum size
        count = 0
        while count < self._min_pool_size:
            # append the connection inside the idle connections
            self._idle_connection.append(self.new_connection())
            count += 1
        LOG.debug("Pool Initialization complete ")
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def get_connection(self):
        '''
            Method to get the connection form the pool.
            The method will return the connection form the pool
            if the connection is there available connections.

            a) Checks if there is idle connection. If available
               it returns the connection adding it inside the
               active connection list and removing from the
               idle connection list.
            b) If the connection is not inside the idle connection
               it will check whether the pool has reached it maximum
               capacity. If not it will create one connection and add
               it to the active connection list and return the connection.
            c) If the connection is not available inside the idle connection
               and pool has reached it maximum limit, it will check whether
               the pool is configured for waiting or not. If the wait_if_busy
               flag is False, the pool will return immediately and will return
               a None object. If wait_if_busy is True, the pool will wait till
               the other thread acquired the lock notify's it.
            d) The wait_interval value determines the value in second to which
               a thread wait. After the wait interval has expired, thread will
               come alive. Once its alive it will check whether the connection
               is available or not in the pool. If the connection is not there
               in the pool, it will return a None object.
            e) User can override the behavior of thread wait interval by
               passing wait interval as a negative value which will make
               the thread wait till it will be notified by any thread which
               has acquired the connection.

            The Pool is synchronized with acquire call to the condition and
            will be locked till it is released. Note: In case of wait, the
            wait will release the lock. Once waiting thread is awakened,
            it will automatically acquire the lock on the condition object
            and hence it must release before returning form the method.

            @return:
                Return a valid connection object if the connection is
                available.
                Return None, in case there are no connection and wait_if_busy
                is false or wait interval has expired and there is not
                connection available in the pool.
        '''
        # acquire the lock on the condition object to prohibit other thread
        # to enter the critical section.
        self._condition.acquire()
        # check whether there are connections inside the idle state.
        if len(self._idle_connection) > 0:
            # if connection is there, pop it and append to active
            # list before returning the connection.
            connection = self._idle_connection.pop()
            # append the connection in active connection list.
            self._active_connection.append(connection)
            # release the lock acquired on the condition.
            self._condition.release()
            LOG.debug("Returned the connection from idle list ")
            LOG.debug("Pool Info \n " + self.get_pool_info())
            # return connection
            return connection
        else:
            # If the connection is not there in the idle connection list
            # check whether pool size has crossed the upper limit.
            if self._max_pool_size > self.get_pool_size():
                # if pool size is less than the max pool size
                # create a new connection
                connection = self.new_connection()
                # append the connection inside the active list
                self._active_connection.append(connection)
                # release the acquired lock
                self._condition.release()
                LOG.debug(
                    "Created the connection as max pool size has not reached ")
                LOG.debug("Pool Info \n " + self.get_pool_info())
                # return the connection
                return connection
            # if connection is not there in the idle connection
            # list and we hve reached the max pool size, check
            # whether the wait if busy flag is False or not.
            elif self._wait_if_busy is False:
                # if use has configure pool not to wait
                # release the acquired lock and return
                # None
                self._condition.release()
                LOG.debug("No connection available in pool"
                          " returning the None connection as "
                          "wait flag is false")
                LOG.debug("Pool Info \n " + self.get_pool_info())
                return None
            else:
                # if user has configured the pool to wait,
                # wait on the condition object till it get
                # notifies if the wait interval is < 0
                # or wait time till interval expires
                if(self._wait_interval < 0):
                    LOG.debug("Waiting for connection to be "
                              "available in the pool")
                    self._condition.wait()
                else:
                    LOG.debug("Waiting for connection to be "
                              "available in the pool")
                    self._condition.wait(self._wait_interval)
                    # check if the connection is available or not
                    # if the connection is not available it means
                    # the waiting thread is timed out and hence
                    # release the connection and return a None
                    if self.is_connection_available() is False:
                        self._condition.release()
                        LOG.debug("Returning None as wait timed out")
                        return None
                # Once notified, the thread will re-acquire the local
                # automatically, so release the lock on condition
                # object
                self._condition.release()
                LOG.debug("Pool Info \n " + self.get_pool_info())
                # Since its been notified, call the get connection method
                # again to execute the complete logic of getting connection.
                return self.get_connection()

    def new_connection(self):
        '''
            A method which provides a feature to create a new connection. The
            method can be used form out side whenever a new connection is
            required.

            The method is meant to be implemented for the custom pool
            implementation. Right now it uses a dummy connection object
            to return. The extending pool class should override the method
            to give the pool specific implementation.

            @return:
                Always return a connection object valid for implementing pool.
        '''
        # return's a dummy connection object
        return Connection(self._host, self._user_name,
                          self._password, self._port)

    def release_connection(self, connection, delete=False):
        '''
            The method is used to release the connection. The assumption
            is whenever we use pool to get the connection we should always
            release the connection after operations.

            The release is necessary to move the connection from the active
            state to the idle state which will serve all the other waiting
            threads or can be use by future incoming request to the pool.

            The method is thread safe and we acquire the lock on the condition
            object before releasing the connection and then release the lock
            for other threads to enter the critical section.

            @param connection:
                connection object to release from the active list.
            @param delete:
                Flag suggest whether to delete the connection or
                move to the idle connection list.

            @return:
                Void method, does not return anything.
        '''
        # acquire the lock on the condition object to synchronize the
        # complete block.
        self._condition.acquire()
        # remove the connection object from the active connection list
        self._active_connection.remove(connection)
        # checks whether the delete flag is True or not
        if delete is False:
            # If False, append the connection inside the idle list to make
            # it available to other threads.
            self._idle_connection.append(connection)
        else:
            # else before deleting make sure connection is closed to free
            # the connection object.
            connection.close_connection()
        # Once connection is released, notify other waiting threads to get
        # a connection.
        # Notify only one thread to wake up as one connection has been
        # released.
        self._condition.notify()
        # release the acquired lock on the condition object.
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def release_all_connection(self, delete=False):
        '''
            The method can be used to release all the connection from the
            connection pool. If the delete flag is passed as True, the
            connections inside the active connection will be release and
            deleted and will not be added inside the idle connection list.
            If the delete flag is false, the connections will be released
            from the active connection list, and will be added to the idle
            connection list.

            @param delete:
                False: if false, the connection will be removed from the active
                       connection list, and will be added to idle connection
                       list.
                True: If true, the connection will be removed from the active
                      connection list and will be closed, without adding inside
                      the idle connection list.
        '''
        # acquire the lock on the condition object, so that no one can enter
        # the method if some thread has already called it.
        self._condition.acquire()
        # run through all the connections inside the active connection list,
        # and remove from the active connection list.
        for connection in self._active_connection[:]:
            self._active_connection.remove(connection)
            # check if the delete flag is true or not.
            # if false, add the connection to idle connection list.
            if delete is False:
                self._idle_connection.append(connection)
            else:
                # else do not add it inside the idle connection list
                # close the connection so that the connection object
                # will not hold the connection.
                connection.close_connection()
            # For one connection removal notify one thread to get the
            # connection.
            self._condition.notify()
        # Once all the connections are navigated, release the acquired lock
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def release_connections(self, connections, delete=False):
        '''
            The method can be used to release a set of connections
            passed inside a list. The method will delete those connections
            if the delete flag is true for ever from the pool or shift it
            from the active connection list to the idle connection list.

            @param connections:
                List of connections object which we want to remove.
            @param delete:
                False: If false, the connection will be moved to idle
                connection list
                True: If true, the connection will be closed and will not be
                      added inside the idle connection list.
        '''
        # acquire the lock so that no other thread will enter the method.
        self._condition.acquire()
        # navigate the connection list for each connection inside it to remove
        for connection in connections:
            # remove the connection from the active connection list.
            self._active_connection.remove(connection)
            # check whether to delete the connection object or not
            if delete is False:
                # if delete flag is false shift to idle connection list
                self._idle_connection.append(connection)
            else:
                # else close the connection object.
                connection.close_connection()
            # notify one thread as one connection has been released.
            self._condition.notify()
        # release the acquired lock on the condition object.
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def close_connection(self, connection, delete=True):
        '''
            This method provides pool user a feature to close the  connection.
            The method will close the connection and will delete it from the
            connection lists managed by the pool. User can override the delete
            behavior with a False flag for delete which will keep a closed
            connection inside the connection list.

            @param connection:
                Connection object to close.
            @param delete:
                False: Will close the connection and keep it inside the
                        conneciton list.
                True: Will close the connection and willl delete from
                        the connection list.
        '''
        # acquire the lock on the condition object
        self._condition.acquire()
        # close the connection
        connection.close_connection()
        # if delete is true then will delete from the lists.
        if delete:
            # delete from the active and idle connection list
            if self._active_connection.index(connection) > 0:
                self._active_connection.remove(connection)
                # if connection is removed from the connection list notify
                # waiting threads
                self._condition.notify()
            elif self._idle_connection.index(connection) > 0:
                self._idle_connection.remove(connection)
                # if connection is removed from the connection list notify
                # waiting threads
                self._condition.notify()
        # release the lock on the condition object
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def close_all_connection(self, delete=True):
        '''
            Connection pool method to close all the connections held inside
            the pool. It will by default close all the connection and will
            delete it from the connection list.

            @param delete:
                True: will delete the connection from the connection list
                    managed by the pool.
                False: will only close the connection and the connection
                    wil reside inside the connection list.
        '''
        # acquire the local on condition object.
        self._condition.acquire()
        # run the loop to close connections from active connection list.
        for connection in self._active_connection[:]:
            # close the connection
            connection.close_connection()
            # if delete flag is true delete it from active connection list
            if delete:
                # remove it from active connection list
                self._active_connection.remove(connection)
                # notify other waiting thread
                self._condition.notify()

        # run the loop on idle connection list.
        for connection in self._idle_connection[:]:
            # close the connection
            connection.close_connection()
            # if delete flag is true delete from the idle connection list.
            if delete:
                # remove from idle connection list
                self._idle_connection.remove(connection)
                # notify other waiting threads.
                self._condition.notify()
        # release the lock on condition object
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def close_connections(self, connections, delete=True):
        '''
            The method is used to close a set of connection from the pool
            If the delete flag is true it will close the connection and
            will delete it from the connection list.

            @param connections:
                list of connections object needs to be closed.
            @param delete:
                True: will delete the connections from the connection
                        list
                False: will keep the connections inside the list.
        '''
        # acquire the lock on the condition object
        self._condition.acquire()
        # loop through the connection list
        for connection in connections:
            # close the connection
            connection.close_connection()
            # if the delete flag is true find the connection and remove
            # from the conneciton lists
            if delete:
                # check if the connection is there inside the active connection
                # list
                if self._active_connection.index(connection) > 0:
                    # remove the connection object from the  active connection
                    # list
                    self._active_connection.remove(connection)
                    # notify other waiting thread
                    self._condition.notify()
                # check if the connection is there in the idle connection list
                elif self._idle_connection.index(connection) > 0:
                    # remove the connection from the idle connection list
                    self._idle_connection.remove(connection)
                    # notify other waiting thread
                    self._condition.notify()
        # release the lock on condition object
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())

    def get_idle_connection_count(self):
        '''
            return the number of idle connections inside the connection pool
        '''
        return len(self._idle_connection)

    def get_active_connection_count(self):
        '''
            returns the number of active connections inside the connection pool
        '''
        return len(self._active_connection)

    def get_pool_size(self):
        '''
            returns the total pool size
        '''
        return len(self._active_connection) + len(self._idle_connection)

    def is_connection_available(self):
        '''
            The method will return a boolean value whether the connection
            is available inside the pool or not.

            @return:
                True: if connection is available in the pool
                False: if connection is not available in the pool.
        '''
        # check the current pool size is less than the max pool size
        # or there is any idle connection in the pool
        if (self.get_idle_connection_count() > 0
                or self.get_pool_size() < self._max_pool_size):
            # if its less then there is a possibility of free connection
            # hence return true
            return True
        else:
            # if the pool size is not less than the map pool size configured
            # return false as there is not connection that can be served form
            # the pool
            return False

    def get_pool_info(self):
        '''
            This method will return the overall statistics of the pool.
            The statistics will show the following things

            a> Idle connections inside the pool
            b> Active connection inside the pool
            c> Total pool size
        '''
        info_str = 'Idle connection : ' + str(self.get_idle_connection_count())
        info_str += '\nActive Connection : ' + \
            str(self.get_active_connection_count())
        info_str += '\nTotal Pool Size : ' + str(self.get_pool_size())
        LOG.debug(info_str)
        return info_str

    def extend_pool_size(self, pool_size):
        '''
            The method will increase max pool size to the provided max
            size.

            @param pool_size:
                pool size which will increase the max cap for the
                pool. It will add the size passed inside the method
                to increase the max pool size.
            @return:
                Will return nothing.
        '''
        # Acquire the lock on the condition object
        self._condition.acquire()
        # Extend the pool size
        self._max_pool_size += pool_size
        # Exactly notify the number of thread waiting
        # to the size
        self._condition.notify(pool_size)
        # release the lock
        self._condition.release()
        LOG.debug("Pool Info \n " + self.get_pool_info())


class SSHConnectionPool(ConnectionPool):

    '''
        SSHConnection Pool to hold the pool of SSH connections
    '''
    ssh_pool = None

    def __init__(self, host, port, user_name, password,
                 known_hosts, min_pool_size=5, max_pool_size=10,
                 wait_if_busy=True, wait_interval=-1,
                 key_file=None, timeout=60):
        '''
            Constructor to initialize the ssh connection pool
        '''
        self._key_file = key_file
        self._timeout = timeout
        self._known_hosts = known_hosts
        super(SSHConnectionPool, self).__init__(user_name, password,
                                                host, port,
                                                min_pool_size, max_pool_size,
                                                wait_if_busy, wait_interval)

    @classmethod
    def get_pool_instance(self, host, port, user_name, password,
                          known_hosts, min_pool_size=5, max_pool_size=10,
                          wait_if_busy=True, wait_interval=-1,
                          key_file=None, timeout=60):
        '''
            Method to get the SSH connection pool instance.
        '''
        if self.ssh_pool is None:
            self.ssh_pool = SSHConnectionPool(host, port,
                                              user_name, password, known_hosts,
                                              min_pool_size=min_pool_size,
                                              max_pool_size=max_pool_size,
                                              wait_if_busy=wait_if_busy,
                                              wait_interval=wait_interval,
                                              key_file=key_file,
                                              timeout=timeout)
        return self.ssh_pool

    def new_connection(self):
        '''
            Overriding the new connection method
            to return the pool specific connection.
        '''
        ssh = SSHConnection(self._host, self._port, self._user_name,
                            self._password, self._key_file, self._timeout,
                            self._known_hosts)
        ssh.open_connection()
        ssh.set_keep_alive(20)
        return ssh
