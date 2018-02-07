import logging
import threading
import uuid

from sqlalchemy import Column, Integer, ForeignKey, String, orm
from sqlalchemy.orm import relationship, backref

from walkoff.appgateway import get_app_action, is_app_action_bound
from walkoff.coredb.argument import Argument
from walkoff.core.actionresult import ActionResult
from walkoff.coredb import Device_Base
from walkoff.events import WalkoffEvent
from walkoff.coredb.executionelement import ExecutionElement
from walkoff.helpers import get_app_action_api, InvalidArgument, format_exception_message
from walkoff.appgateway.validator import validate_app_action_parameters
from walkoff.dbtypes import Guid
logger = logging.getLogger(__name__)


class Action(ExecutionElement, Device_Base):

    __tablename__ = 'action'
    _workflow_id = Column(Guid(), ForeignKey('workflow.id'))
    app_name = Column(String(80), nullable=False)
    action_name = Column(String(80), nullable=False)
    name = Column(String(80))
    device_id = Column(Integer)
    arguments = relationship('Argument', backref=backref('_action'), cascade='all, delete, delete-orphan')
    triggers = relationship('Condition', backref=backref('_action'), cascade='all, delete-orphan')
    position = relationship('Position', uselist=False, backref=backref('_action'), cascade='all, delete-orphan')

    def __init__(self, app_name, action_name, name, device_id=None, id=None, arguments=None, triggers=None,
                 position=None):
        """Initializes a new Action object. A Workflow has many actions that it executes.

        Args:
            app_name (str): The name of the app associated with the Action
            action_name (str): The name of the action associated with a Action
            name (str): The name of the Action object.
            device_id (int, optional): The id of the device associated with the app associated with the Action. Defaults
                to None.
            arguments (list[Argument], optional): A list of Argument objects that are parameters to the action.
                Defaults to None.
            triggers (list[Condition], optional): A list of Condition objects for the Action. If a Action should wait
                for data before continuing, then include these Trigger objects in the Action init. Defaults to None.
            position (Position, optional): Position object for the Action. Defaults to None.
        """
        ExecutionElement.__init__(self, id)

        self.triggers = []
        if triggers:
            for trigger in triggers:
                self.triggers.append(trigger)

        self.name = name
        self.device_id = device_id
        self.app_name = app_name
        self.action_name = action_name

        self._run, self._arguments_api = get_app_action_api(self.app_name, self.action_name)
        if is_app_action_bound(self.app_name, self._run) and not self.device_id:
            raise InvalidArgument(
                "Cannot initialize Action {}. App action is bound but no device ID was provided.".format(self.name))

        validate_app_action_parameters(self._arguments_api, arguments, self.app_name, self.action_name)

        self.arguments = []
        if arguments:
            self.arguments = arguments

        self.position = position

        self._incoming_data = None
        self._event = threading.Event()
        self._output = None
        self._execution_uid = 'default'
        self._action_executable = get_app_action(self.app_name, self._run)

    @orm.reconstructor
    def init_on_load(self):
        self._run, self._arguments_api = get_app_action_api(self.app_name, self.action_name)
        self._output = None
        self._action_executable = get_app_action(self.app_name, self._run)
        self._execution_uid = 'default'

    def get_output(self):
        """Gets the output of an Action (the result)

        Returns:
            The result of the Action
        """
        return self._output

    def get_execution_uid(self):
        """Gets the execution UID of the Action

        Returns:
            The execution UID
        """
        return self._execution_uid

    def set_arguments(self, new_arguments):
        """Updates the arguments for an Action object.

        Args:
            new_arguments ([Argument]): The new Arguments for the Action object.
        """
        validate_app_action_parameters(self._arguments_api, new_arguments, self.app_name, self.action_name)
        self.arguments = new_arguments

    def execute(self, instance, accumulator, arguments=None, resume=False):
        """Executes an Action by calling the associated app function.

        Args:
            instance (App): The instance of an App object to be used to execute the associated function.
            accumulator (dict): Dict containing the results of the previous actions
            arguments (list[Argument]): Optional list of Arguments to be used if the Action is the starting step of
                the Workflow. Defaults to None.
            resume (bool, optional): Optional boolean to resume a previously paused workflow. Defaults to False.

        Returns:
            The result of the executed function.
        """
        self._execution_uid = str(uuid.uuid4())

        WalkoffEvent.CommonWorkflowSignal.send(self, event=WalkoffEvent.ActionStarted)

        if self.triggers and not resume:
            print("Action has triggers, sending signal and returning")
            WalkoffEvent.CommonWorkflowSignal.send(self, event=WalkoffEvent.TriggerActionAwaitingData)
            logger.debug('Trigger Action {} is awaiting data'.format(self.name))
            self._output = None
            return {"trigger": "trigger"}

        arguments = arguments if arguments else self.arguments

        try:
            args = validate_app_action_parameters(self._arguments_api, arguments, self.app_name, self.action_name,
                                                  accumulator=accumulator)
            if is_app_action_bound(self.app_name, self._run):
                result = self._action_executable(instance, **args)
            else:
                result = self._action_executable(**args)
            result.set_default_status(self.app_name, self.action_name)
            if result.is_failure(self.app_name, self.action_name):
                WalkoffEvent.CommonWorkflowSignal.send(self, event=WalkoffEvent.ActionExecutionError,
                                                       data=result.as_json())
            else:
                WalkoffEvent.CommonWorkflowSignal.send(self, event=WalkoffEvent.ActionExecutionSuccess,
                                                       data=result.as_json())
        except Exception as e:
            self.__handle_execution_error(e)
        else:
            self._output = result
            logger.debug(
                'Action {0}-{1} (id {2}) executed successfully'.format(self.app_name, self.action_name, self.id))
            return result

    def __handle_execution_error(self, e):
        formatted_error = format_exception_message(e)
        if isinstance(e, InvalidArgument):
            event = WalkoffEvent.ActionArgumentsInvalid
            return_type = 'InvalidArguments'
        else:
            event = WalkoffEvent.ActionExecutionError
            return_type = 'UnhandledException'
        logger.error('Error calling action {0}. Error: {1}'.format(self.name, formatted_error))
        self._output = ActionResult('error: {0}'.format(formatted_error), return_type)
        WalkoffEvent.CommonWorkflowSignal.send(self, event=event, data=self._output.as_json())

    def execute_trigger(self, data_in, accumulator):
        if all(trigger.execute(data_in=data_in, accumulator=accumulator) for trigger in self.triggers):
            logger.debug('Trigger is valid for input {0}'.format(data_in))
            return True
        else:
            logger.debug('Trigger is not valid for input {0}'.format(data_in))
            return False

    def __get_argument_by_name(self, name):
        for argument in self.arguments:
            if argument.name == name:
                return argument
        return None
