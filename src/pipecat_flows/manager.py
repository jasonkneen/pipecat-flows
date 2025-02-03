#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Core conversation flow management system.

This module provides the FlowManager class which orchestrates conversations
across different LLM providers. It supports:
- Static flows with predefined paths
- Dynamic flows with runtime-determined transitions
- State management and transitions
- Function registration and execution
- Action handling
- Cross-provider compatibility

The flow manager coordinates all aspects of a conversation, including:
- LLM context management
- Function registration
- State transitions
- Action execution
- Error handling
"""

import asyncio
import copy
import inspect
import sys
import warnings
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Union, cast

from loguru import logger
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMMessagesAppendFrame,
    LLMMessagesUpdateFrame,
    LLMSetToolsFrame,
)
from pipecat.pipeline.task import PipelineTask

from .actions import ActionError, ActionManager
from .adapters import create_adapter
from .exceptions import FlowError, FlowInitializationError, FlowTransitionError
from .types import (
    ActionConfig,
    ContextStrategy,
    ContextStrategyConfig,
    FlowArgs,
    FlowConfig,
    FlowResult,
    NodeConfig,
)

if TYPE_CHECKING:
    from pipecat.services.anthropic import AnthropicLLMService
    from pipecat.services.google import GoogleLLMService
    from pipecat.services.openai import OpenAILLMService

    LLMService = Union[OpenAILLMService, AnthropicLLMService, GoogleLLMService]
else:
    LLMService = Any


class FlowManager:
    """Manages conversation flows, supporting both static and dynamic configurations.

    The FlowManager orchestrates conversation flows by managing state transitions,
    function registration, and message handling across different LLM providers.

    Attributes:
        task: Pipeline task for frame queueing
        llm: LLM service instance (OpenAI, Anthropic, or Google)
        state: Shared state dictionary across nodes
        current_node: Currently active node identifier
        initialized: Whether the manager has been initialized
        nodes: Node configurations for static flows
        current_functions: Currently registered function names
    """

    def __init__(
        self,
        *,
        task: PipelineTask,
        llm: LLMService,
        context_aggregator: Any,
        tts: Optional[Any] = None,
        flow_config: Optional[FlowConfig] = None,
        context_strategy: Optional[ContextStrategyConfig] = None,
    ):
        """Initialize the flow manager.

        Args:
            task: PipelineTask instance for queueing frames
            llm: LLM service instance (e.g., OpenAI, Anthropic)
            context_aggregator: Context aggregator for updating user context
            tts: Optional TTS service for voice actions (deprecated)
            flow_config: Optional static flow configuration. If provided,
                operates in static mode with predefined nodes
            context_strategy: Optional context strategy configuration

        Raises:
            ValueError: If any transition handler is not a valid async callable
        Deprecated:
            0.0.13: The `tts` parameter is deprecated and will be removed in a future version.
        """
        if tts is not None:
            warnings.warn(
                "The 'tts' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.task = task
        self.llm = llm
        self.action_manager = ActionManager(task)
        self.adapter = create_adapter(llm)
        self.initialized = False
        self._context_aggregator = context_aggregator
        self._pending_function_calls = 0
        self._context_strategy = context_strategy or ContextStrategyConfig(
            strategy=ContextStrategy.APPEND
        )

        # Set up static or dynamic mode
        if flow_config:
            self.nodes = flow_config["nodes"]
            self.initial_node = flow_config["initial_node"]
            logger.debug("Initialized in static mode")
        else:
            self.nodes = {}
            self.initial_node = None
            logger.debug("Initialized in dynamic mode")

        self.state: Dict[str, Any] = {}  # Shared state across nodes
        self.current_functions: Set[str] = set()  # Track registered functions
        self.current_node: Optional[str] = None

    def _validate_transition_callback(self, name: str, callback: Any) -> None:
        """Validate a transition callback.

        Args:
            name: Name of the function the callback is for
            callback: The callback to validate

        Raises:
            ValueError: If callback is not a valid async callable
        """
        if not callable(callback):
            raise ValueError(f"Transition callback for {name} must be callable")
        if not inspect.iscoroutinefunction(callback):
            raise ValueError(f"Transition callback for {name} must be async")

    async def initialize(self) -> None:
        """Initialize the flow manager."""
        if self.initialized:
            logger.warning(f"{self.__class__.__name__} already initialized")
            return

        try:
            self.initialized = True
            logger.debug(f"Initialized {self.__class__.__name__}")

            # If in static mode, set initial node
            if self.initial_node:
                logger.debug(f"Setting initial node: {self.initial_node}")
                await self.set_node(self.initial_node, self.nodes[self.initial_node])

        except Exception as e:
            self.initialized = False
            raise FlowInitializationError(f"Failed to initialize flow: {str(e)}") from e

    def get_current_context(self) -> List[dict]:
        """Get the current conversation context.

        Returns:
            List of messages in the current context, including system messages,
            user messages, and assistant responses.

        Raises:
            FlowError: If context aggregator is not available
        """
        if not self._context_aggregator:
            raise FlowError("No context aggregator available")

        return self._context_aggregator.user()._context.messages

    def register_action(self, action_type: str, handler: Callable) -> None:
        """Register a handler for a specific action type.

        Args:
            action_type: String identifier for the action (e.g., "tts_say")
            handler: Async or sync function that handles the action

        Example:
            async def custom_notification(action: dict):
                text = action.get("text", "")
                await notify_user(text)

            flow_manager.register_action("notify", custom_notification)
        """
        self.action_manager._register_action(action_type, handler)

    def _register_action_from_config(self, action: ActionConfig) -> None:
        """Register an action handler from action configuration.

        Args:
            action: Action configuration dictionary containing type and optional handler

        Raises:
            ActionError: If action type is not registered and no valid handler provided
        """
        action_type = action.get("type")
        handler = action.get("handler")

        # Register action if not already registered
        if action_type and action_type not in self.action_manager.action_handlers:
            # Register handler if provided
            if handler and callable(handler):
                self.register_action(action_type, handler)
                logger.debug(f"Registered action handler from config: {action_type}")
            # Raise error if no handler provided and not a built-in action
            elif action_type not in ["tts_say", "end_conversation"]:
                raise ActionError(
                    f"Action '{action_type}' not registered. "
                    "Provide handler in action config or register manually."
                )

    async def _call_handler(self, handler: Callable, args: FlowArgs) -> FlowResult:
        """Call handler with or without args based on its signature.

        Args:
            handler: The function to call
            args: Arguments dictionary

        Returns:
            Dict[str, Any]: Handler result
        """
        sig = inspect.signature(handler)
        if "args" in sig.parameters:
            return await handler(args)
        return await handler()

    async def _handle_static_transition(
        self,
        function_name: str,
        args: Dict[str, Any],
        flow_manager: "FlowManager",
    ) -> None:
        """Handle transitions for static flows.

        Transitions to a new node in static flows by looking up the node
        configuration and setting it as the current node. Logs a warning
        if the target node is not found in the flow configuration.

        Args:
            function_name: Name of the target node to transition to
            args: Arguments passed to the function that triggered the transition
            flow_manager: Reference to the FlowManager instance
        """
        if function_name in self.nodes:
            logger.debug(f"Static transition to node: {function_name}")
            await self.set_node(function_name, self.nodes[function_name])
        else:
            logger.warning(f"Static transition failed: Node '{function_name}' not found")

    async def _create_transition_func(
        self,
        name: str,
        handler: Optional[Callable],
        transition_to: Optional[str],
        transition_callback: Optional[Callable] = None,
    ) -> Callable:
        """Create a transition function for the given name and handler.

        Args:
            name: Name of the function being registered
            handler: Optional function to process data
            transition_to: Optional node to transition to (static flows)
            transition_callback: Optional callback for dynamic transitions

        Returns:
            Callable: Async function that handles the tool invocation

        Raises:
            ValueError: If both transition_to and transition_callback are specified
        """
        if transition_to and transition_callback:
            raise ValueError(
                f"Function {name} cannot have both transition_to and transition_callback"
            )

        # Validate transition callback if provided
        if transition_callback:
            self._validate_transition_callback(name, transition_callback)

        is_edge_function = bool(transition_to) or bool(transition_callback)

        def decrease_pending_function_calls() -> None:
            """Decrease the pending function calls counter if greater than zero."""
            if self._pending_function_calls > 0:
                self._pending_function_calls -= 1
                logger.debug(
                    f"Function call completed: {name} (remaining: {self._pending_function_calls})"
                )

        async def on_context_updated_edge(args: Dict[str, Any], result_callback: Callable) -> None:
            """Handle context updates for edge functions with transitions."""
            try:
                decrease_pending_function_calls()

                # Only process transition if this was the last pending call
                if self._pending_function_calls == 0:
                    if transition_to:  # Static flow
                        logger.debug(f"Static transition to: {transition_to}")
                        await self.set_node(transition_to, self.nodes[transition_to])
                    elif transition_callback:  # Dynamic flow
                        logger.debug(f"Dynamic transition for: {name}")
                        await transition_callback(args, self)
                    # Reset counter after transition completes
                    self._pending_function_calls = 0
                    logger.debug("Reset pending function calls counter")
                else:
                    logger.debug(
                        f"Skipping transition, {self._pending_function_calls} calls still pending"
                    )
            except Exception as e:
                logger.error(f"Error in transition: {str(e)}")
                self._pending_function_calls = 0
                await result_callback(
                    {"status": "error", "error": str(e)},
                    properties=None,  # Clear properties to prevent further callbacks
                )
                raise  # Re-raise to prevent further processing

        async def on_context_updated_node() -> None:
            """Handle context updates for node functions without transitions."""
            decrease_pending_function_calls()

        async def transition_func(
            function_name: str,
            tool_call_id: str,
            args: Dict[str, Any],
            llm: Any,
            context: Any,
            result_callback: Callable,
        ) -> None:
            """Inner function that handles the actual tool invocation."""
            try:
                # Track pending function call
                self._pending_function_calls += 1
                logger.debug(
                    f"Function call pending: {name} (total: {self._pending_function_calls})"
                )

                # Execute handler if present
                if handler:
                    result = await self._call_handler(handler, args)
                    logger.debug(f"Handler completed for {name}")
                else:
                    result = {"status": "acknowledged"}
                    logger.debug(f"Function called without handler: {name}")

                # For edge functions, prevent LLM completion until transition (run_llm=False)
                # For node functions, allow immediate completion (run_llm=True)
                async def on_context_updated() -> None:
                    if is_edge_function:
                        await on_context_updated_edge(args, result_callback)
                    else:
                        await on_context_updated_node()

                properties = FunctionCallResultProperties(
                    run_llm=not is_edge_function,
                    on_context_updated=on_context_updated,
                )
                await result_callback(result, properties=properties)

            except Exception as e:
                logger.error(f"Error in transition function {name}: {str(e)}")
                self._pending_function_calls = 0
                error_result = {"status": "error", "error": str(e)}
                await result_callback(error_result)

        return transition_func

    def _lookup_function(self, func_name: str) -> Callable:
        """Look up a function by name in the main module.

        Args:
            func_name: Name of the function to look up

        Returns:
            Callable: The found function

        Raises:
            FlowError: If function is not found
        """
        main_module = sys.modules["__main__"]
        handler = getattr(main_module, func_name, None)

        if handler is not None:
            logger.debug(f"Found function '{func_name}' in main module")
            return handler

        error_message = (
            f"Function '{func_name}' not found in main module.\n"
            "Ensure the function is defined in your main script "
            "or imported into it."
        )

        raise FlowError(error_message)

    async def _register_function(
        self,
        name: str,
        new_functions: Set[str],
        handler: Optional[Callable],
        transition_to: Optional[str] = None,
        transition_callback: Optional[Callable] = None,
    ) -> None:
        """Register a function with the LLM if not already registered.

        Args:
            name: Name of the function to register with the LLM
            handler: Either a callable function or a string. If string starts with
                    '__function__:', extracts the function name after the prefix
            transition_to: Optional node name to transition to after function execution
            transition_callback: Optional callback for dynamic transitions
            new_functions: Set to track newly registered functions for this node

        Raises:
            FlowError: If function registration fails or handler lookup fails
        """
        if name not in self.current_functions:
            try:
                # Handle special token format (e.g. "__function__:function_name")
                if isinstance(handler, str) and handler.startswith("__function__:"):
                    func_name = handler.split(":")[1]
                    handler = self._lookup_function(func_name)

                self.llm.register_function(
                    name,
                    await self._create_transition_func(
                        name, handler, transition_to, transition_callback
                    ),
                )
                new_functions.add(name)
                logger.debug(f"Registered function: {name}")
            except Exception as e:
                logger.error(f"Failed to register function {name}: {str(e)}")
                raise FlowError(f"Function registration failed: {str(e)}") from e

    def _remove_handlers(self, tool_config: Dict[str, Any]) -> None:
        """Remove handlers from tool configuration.

        Args:
            tool_config: Function configuration to clean
        """
        if "function" in tool_config and "handler" in tool_config["function"]:
            del tool_config["function"]["handler"]
        elif "handler" in tool_config:
            del tool_config["handler"]
        elif "function_declarations" in tool_config:
            for decl in tool_config["function_declarations"]:
                if "handler" in decl:
                    del decl["handler"]

    def _remove_transition_info(self, tool_config: Dict[str, Any]) -> None:
        """Remove transition information from tool configuration.

        Removes transition_to and transition_callback fields to prevent them from being
        sent to the LLM provider.

        Args:
            tool_config: Function configuration to clean
        """
        if "function" in tool_config:
            # Clean OpenAI format
            if "transition_to" in tool_config["function"]:
                del tool_config["function"]["transition_to"]
            if "transition_callback" in tool_config["function"]:
                del tool_config["function"]["transition_callback"]
        elif "function_declarations" in tool_config:
            # Clean Gemini format
            for decl in tool_config["function_declarations"]:
                if "transition_to" in decl:
                    del decl["transition_to"]
                if "transition_callback" in decl:
                    del decl["transition_callback"]
        else:
            # Clean Anthropic format
            if "transition_to" in tool_config:
                del tool_config["transition_to"]
            if "transition_callback" in tool_config:
                del tool_config["transition_callback"]

    async def set_node(self, node_id: str, node_config: NodeConfig) -> None:
        """Set up a new conversation node and transition to it.

        Handles the complete node transition process in the following order:
        1. Execute pre-actions (if any)
        2. Set up messages (role and task)
        3. Register node functions
        4. Update LLM context with messages and tools
        5. Update state (current node and functions)
        6. Trigger LLM completion with new context
        7. Execute post-actions (if any)

        Args:
            node_id: Identifier for the new node
            node_config: Complete configuration for the node

        Raises:
            FlowTransitionError: If manager not initialized
            FlowError: If node setup fails
        """
        if not self.initialized:
            raise FlowTransitionError(f"{self.__class__.__name__} must be initialized first")

        try:
            self._validate_node_config(node_id, node_config)
            logger.debug(f"Setting node: {node_id}")

            # Register action handlers from config
            for action_list in [
                node_config.get("pre_actions", []),
                node_config.get("post_actions", []),
            ]:
                for action in action_list:
                    self._register_action_from_config(action)

            # Execute pre-actions if any
            if pre_actions := node_config.get("pre_actions"):
                await self._execute_actions(pre_actions=pre_actions)

            # Combine role and task messages
            messages = []
            if role_messages := node_config.get("role_messages"):
                messages.extend(role_messages)
            messages.extend(node_config["task_messages"])

            # Register functions and prepare tools
            tools = []
            new_functions: Set[str] = set()

            for func_config in node_config["functions"]:
                # Handle Gemini's nested function declarations
                if "function_declarations" in func_config:
                    for declaration in func_config["function_declarations"]:
                        name = declaration["name"]
                        handler = declaration.get("handler")
                        transition_to = declaration.get("transition_to")
                        transition_callback = declaration.get("transition_callback")
                        logger.debug(f"Processing function: {name}")
                        await self._register_function(
                            name=name,
                            new_functions=new_functions,
                            handler=handler,
                            transition_to=transition_to,
                            transition_callback=transition_callback,
                        )
                else:
                    name = self.adapter.get_function_name(func_config)
                    logger.debug(f"Processing function: {name}")

                    # Extract handler and transition info based on format
                    if "function" in func_config:
                        handler = func_config["function"].get("handler")
                        transition_to = func_config["function"].get("transition_to")
                        transition_callback = func_config["function"].get("transition_callback")
                    else:
                        handler = func_config.get("handler")
                        transition_to = func_config.get("transition_to")
                        transition_callback = func_config.get("transition_callback")

                    await self._register_function(
                        name=name,
                        new_functions=new_functions,
                        handler=handler,
                        transition_to=transition_to,
                        transition_callback=transition_callback,
                    )

                # Create tool config (after removing handler and transition info)
                tool_config = copy.deepcopy(func_config)
                self._remove_handlers(tool_config)
                self._remove_transition_info(tool_config)
                tools.append(tool_config)

            # Let adapter format tools for provider
            formatted_tools = self.adapter.format_functions(tools)

            # Update LLM context
            await self._update_llm_context(
                messages, formatted_tools, strategy=node_config.get("context_strategy")
            )
            logger.debug("Updated LLM context")

            # Update state
            self.current_node = node_id
            self.current_functions = new_functions

            # Trigger completion with new context
            if self._context_aggregator:
                await self.task.queue_frames([self._context_aggregator.user().get_context_frame()])

            # Execute post-actions if any
            if post_actions := node_config.get("post_actions"):
                await self._execute_actions(post_actions=post_actions)

            logger.debug(f"Successfully set node: {node_id}")

        except Exception as e:
            logger.error(f"Error setting node {node_id}: {str(e)}")
            raise FlowError(f"Failed to set node {node_id}: {str(e)}") from e

    async def _create_conversation_summary(
        self, summary_prompt: str, messages: List[dict]
    ) -> Optional[str]:
        """Generate a conversation summary from messages."""
        return await self.adapter.generate_summary(self.llm, summary_prompt, messages)

    async def _update_llm_context(
        self,
        messages: List[dict],
        functions: List[dict],
        strategy: Optional[ContextStrategyConfig] = None,
    ) -> None:
        """Update LLM context with new messages and functions.

        Args:
            messages: New messages to add to context
            functions: New functions to make available
            strategy: Optional context update configuration

        Raises:
            FlowError: If context update fails
        """
        try:
            update_config = strategy or self._context_strategy

            if (
                update_config.strategy == ContextStrategy.RESET_WITH_SUMMARY
                and self._context_aggregator
                and self._context_aggregator.user()._context.messages
            ):
                # We know summary_prompt exists because of __post_init__ validation in ContextStrategyConfig
                summary_prompt = cast(str, update_config.summary_prompt)
                try:
                    # Try to get summary with 5 second timeout
                    summary = await asyncio.wait_for(
                        self._create_conversation_summary(
                            summary_prompt,
                            self._context_aggregator.user()._context.messages,
                        ),
                        timeout=5.0,
                    )

                    if summary:
                        summary_message = self.adapter.format_summary_message(summary)
                        messages.insert(0, summary_message)
                        logger.debug("Added conversation summary to context")
                    else:
                        # Fall back to RESET strategy if summary fails
                        logger.warning("Failed to generate summary, falling back to RESET strategy")
                        update_config.strategy = ContextStrategy.RESET

                except asyncio.TimeoutError:
                    logger.warning("Summary generation timed out, falling back to RESET strategy")
                    update_config.strategy = ContextStrategy.RESET

            # For first node or RESET/RESET_WITH_SUMMARY strategy, use update frame
            frame_type = (
                LLMMessagesUpdateFrame
                if self.current_node is None
                or update_config.strategy
                in [ContextStrategy.RESET, ContextStrategy.RESET_WITH_SUMMARY]
                else LLMMessagesAppendFrame
            )

            await self.task.queue_frames(
                [frame_type(messages=messages), LLMSetToolsFrame(tools=functions)]
            )

            logger.debug(
                f"Updated LLM context using {frame_type.__name__} with strategy {update_config.strategy}"
            )

        except Exception as e:
            logger.error(f"Failed to update LLM context: {str(e)}")
            raise FlowError(f"Context update failed: {str(e)}") from e

    async def _execute_actions(
        self,
        pre_actions: Optional[List[ActionConfig]] = None,
        post_actions: Optional[List[ActionConfig]] = None,
    ) -> None:
        """Execute pre and post actions.

        Args:
            pre_actions: Actions to execute before context update
            post_actions: Actions to execute after context update
        """
        if pre_actions:
            await self.action_manager.execute_actions(pre_actions)
        if post_actions:
            await self.action_manager.execute_actions(post_actions)

    def _validate_node_config(self, node_id: str, config: NodeConfig) -> None:
        """Validate the configuration of a conversation node.

        This method ensures that:
        1. Required fields (task_messages, functions) are present
        2. Functions have valid configurations based on their type:
        - Node functions must have either a handler or transition_to
        - Edge functions (matching node names) are allowed without handlers
        3. Function configurations match the LLM provider's format

        Args:
            node_id: Identifier for the node being validated
            config: Complete node configuration to validate

        Raises:
            ValueError: If configuration is invalid or missing required fields
        """
        # Check required fields
        if "task_messages" not in config:
            raise ValueError(f"Node '{node_id}' missing required 'task_messages' field")
        if "functions" not in config:
            raise ValueError(f"Node '{node_id}' missing required 'functions' field")

        # Validate each function configuration
        for func in config["functions"]:
            try:
                name = self.adapter.get_function_name(func)
            except KeyError:
                raise ValueError(f"Function in node '{node_id}' missing name field")

            # Skip validation for edge functions (matching node names)
            if name in self.nodes:
                continue

            # Check for handler in provider-specific formats
            has_handler = (
                ("function" in func and "handler" in func["function"])  # OpenAI format
                or "handler" in func  # Anthropic format
                or (  # Gemini format
                    "function_declarations" in func
                    and func["function_declarations"]
                    and "handler" in func["function_declarations"][0]
                )
            )

            # Check for transition_to in provider-specific formats
            has_transition_to = (
                ("function" in func and "transition_to" in func["function"])
                or "transition_to" in func
                or (
                    "function_declarations" in func
                    and func["function_declarations"]
                    and "transition_to" in func["function_declarations"][0]
                )
            )

            # Check for transition_callback in provider-specific formats
            has_transition_callback = (
                ("function" in func and "transition_callback" in func["function"])
                or "transition_callback" in func
                or (
                    "function_declarations" in func
                    and func["function_declarations"]
                    and "transition_callback" in func["function_declarations"][0]
                )
            )

            # Warn if function has no handler or transitions
            if not has_handler and not has_transition_to and not has_transition_callback:
                logger.warning(
                    f"Function '{name}' in node '{node_id}' has neither handler, transition_to, nor transition_callback"
                )
