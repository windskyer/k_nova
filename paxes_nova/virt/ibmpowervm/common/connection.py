# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

import paramiko

from nova.openstack.common import log as logging

from paxes_nova import _

LOG = logging.getLogger(__name__)


class Connection(object):

    '''
        A connection object. This is a abstract object which needs
        to be extended as per the connection need.
        The Connection pool API use this dummy object.

        The custom pool should override the new_connection method
        for the pool and return the custom connection extended from this
        class.
    '''

    def __init__(self, host='', username='', password='', port=22):
        '''
            Constructor to initialize the connection object.

            @param host:
                Host name for the connection
            @param username:
                User name to connection the connection object
            @param password:
                Password for the connection object
            @port:
                Port for the connection object
        '''
        # Initialize the host for connection object
        self._host = host
        # Initialize the user name for the connection object
        self._username = username
        # Initialize the password for the connection object
        self._password = password
        # Initialize the port for the connection object
        self._port = port

    def get_info(self):
        '''
            The method will return the information related to connection.
            Custom connection object may implement this method to return
            a custom information for connection as per there need.

            @return:
                Should return the connection information as string.
                Method should not be void.
        '''
        pass

    def is_secured(self):
        '''
            The method return's  a boolean value suggesting whether
            the connection object is secured or not.
            A custom connection may implement this method if the
            connection is going to be a secured connection. Should
            return true if the connection is secured else false.

            @return:
                Should return a boolean value suggesting whether the
                connection is secured or not.
                True: if connection is secured
                False: if connection is not secured
        '''
        pass

    def close_connection(self):
        '''
            A method to close the connection. The implementing class
            should give the custom logic to close the connection.
            The method can be void or return a boolean value.

            A custom implementation should override this method to cleanly
            close the connection. Closing the connection is necessary to free
            the connection.

            @return:
                A boolean value to indicate if the connection closure
                is successful or not.

                True: if the connection closure successful.
                False: If the connection closure is not successful.
            @raise exception:
                if not implemented the base class will raise a not implemented
                exception.
        '''
        raise NotImplemented()

    def open_connection(self):
        '''
            A method which provide an outer interface to the user or
            implementing class to open / connect the connection.
            An implementing class should implement this method to
            give the custom implementation to open the connection.

            The method may return a boolean value to indicate if the opening of
            the connection is successful or not.

            @return:
                Return a boolean value to indicate whether the open operation
                is successful or not.
                True: Return true if connection opened is successful
                False: Return false if the open operation is not successful

            @raise:
                If not implemented it will raise a not implemented exception
        '''
        raise NotImplemented()

    def get_hostname(self):
        '''
            A method which will return the host name for the connection.
            The method should be implemented by the custom connection
            class if there is some logic to generate the hostname else
            the Connection class has a constructor to initialize these
            parameters and return it. A implementing class should always
            pass the host name inside the constructor.

            @return:
                returns the host name for the connection as string.
        '''
        return self._host

    def get_username(self):
        '''
            A method to return the user name associated with the connection.
            The method can be implemented by the implementing class to return
            the user name if there is some custom logic to generate the user
            for a connection object. The user name should be assigned to the
            Connection class to initialize the connection object.
            If the implementing class does not have a custom method to generate
            they can use the method from the Connection class itself.

            @return:
                user name for the connection as string.
        '''
        return self._username

    def get_port(self):
        '''
            A method to return the port associated with a connection.
            An implementing class should override this method if there
            is a separated process to return port.
            The port should be initialized inside the constructor of
            the Connection class by the implementing class.

            @return:
                An integer value for port associated with connection object.
        '''
        return self._port

    def get_password(self):
        '''
            A method to return the password for the connection.
            The method should return the password as plain string.
            The implementing class should override this
            method if they have some custom logic to generate the password.
            The password should always be initialized inside the constructor
            of the connection object.

            @return:
                password for connection object as string
        '''
        return self._password

    def get_protocol(self):
        '''
            A method to return the protocol associated with the connection. An
            implementing class should give custom implementation for it to
            return the protocol for the connection.

            @return:
                A string with the protocol name
            @raise:
                If the method is not implemented the base class will raise a
                Not implemented exception.
        '''
        raise NotImplemented

    def get_url(self):
        '''
            A method to return the URL coressponding to the connection.
            The method is not compulsory to implement if its not valid
            for a connection.

            @return:
                Return URL as string object
        '''
        pass

    def is_closed(self):
        '''
            A method to return a boolean to indicate whether the connection
            is closed or not. An implementing class should give the
            implementation for it so that user of the API should know whether
            the connection is opened or not.

            @return:
                True: if the connection is opened
                False: if the connection is not open
            @raise:
                Will raise a not implemented exception.
        '''
        raise NotImplemented()

    def set_keep_alive(self, keep_alove_interval):
        '''
            A method to configure the connection to send the keep
            alive signal in specific interval. If a custom connection
            supports the keep alive signal implementing class should
            give its implementation.

            The method returns nothing.

            @param keep_alive_interval:
                Interval for keep alive signal in seconds.

        '''
        pass


class SSHConnection(Connection):

    '''
        The class is a wrapper over paramiko ssh connection
        API for SSH Connections.
    '''

    def __init__(self, host, port, user_name, password, key_file, time_out,
                 known_hosts):
        '''
            Connection constructor for SSH
        '''
        super(SSHConnection, self).__init__(host, user_name, password, port)
        self._key_file = key_file
        self._time_out = time_out
        self._ssh_connection = paramiko.SSHClient()
        self._ssh_connection.load_host_keys(known_hosts)
        self._ssh_connection.set_missing_host_key_policy(
            paramiko.AutoAddPolicy())

    def get_info(self):
        '''
            Return's information related to connection
        '''
        pass

    def is_secured(self):
        '''
            Override method return's true whether its secured or not.
        '''
        return True

    def close_connection(self):
        '''
            Close the ssh connection
        '''
        self._ssh_connection.close()

    def open_connection(self):
        '''
            Open the ssh connection
        '''
        try:
            self._ssh_connection.connect(self._host,
                                         username=self._username,
                                         password=self._password,
                                         port=self._port,
                                         key_filename=self._key_file,
                                         timeout=self._time_out)
        except Exception as ex:
            LOG.exception(_('Unable to connect to PowerVM manager.'))
            raise paramiko.SSHException()

    def get_protocol(self):
        '''
            Returns the protocol for connection
        '''
        return 'ssh'

    def is_closed(self):
        '''
            Returns boolean whether the connection
            is closed or not
        '''
        transport = self._ssh_connection.get_transport()
        if (not transport) or (not transport.is_active()):
            return True
        else:
            return False

    def set_keep_alive(self, keep_alive_interval):
        '''
            Set the keep alive interval for connection
        '''
        self._ssh_connection.get_transport().set_keepalive(keep_alive_interval)

    def open_session(self):
        '''
            Return the connection session
        '''
        return self._ssh_connection.get_transport().open_session()

    def exec_command(self, command):
        return self._ssh_connection.exec_command(command)
