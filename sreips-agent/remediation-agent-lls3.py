"""  
OCP MCP Remediation Agent API  
FastAPI service for automated Kubernetes issue remediation using OCP MCP tools  
Supports multiple issue types: pod_crashloop, oom, image_pull_backoff, quota_exceeded, etc.
"""  
  
from llama_stack_client import LlamaStackClient, Agent  
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
import os  
import uuid  
import logging  
import json  
import re  
import time  
import threading
import requests
import uvicorn

logging.basicConfig(  
    level=logging.DEBUG,  
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'  
)  
logger = logging.getLogger(__name__)

# Global agent instance
remediation_agent = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI startup/shutdown"""
    # Startup
    initialize_agent()
    yield
    # Shutdown (if needed)
    pass

app = FastAPI(title="OCP MCP Remediation Agent API", lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors with detailed messages"""
    # Handle body - decode bytes if present
    body_value = None
    if hasattr(exc, 'body') and exc.body is not None:
        if isinstance(exc.body, bytes):
            try:
                # Try to decode as UTF-8
                body_value = exc.body.decode('utf-8')
            except UnicodeDecodeError:
                # If decoding fails, show as base64 or hex
                body_value = f"<binary data: {len(exc.body)} bytes>"
        else:
            body_value = exc.body
    
    # Check content type to provide helpful error message
    content_type = request.headers.get("content-type", "")
    error_message = "Validation error"
    if "application/x-www-form-urlencoded" in content_type:
        error_message = "This endpoint expects JSON. Received form-encoded data (possibly a Slack webhook). Please send JSON with Content-Type: application/json"
    
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "body": body_value,
            "error_message": error_message,
            "content_type": content_type
        }
    )

class RemediationRequest(BaseModel):
    """Generic remediation request for all Kubernetes issue types"""
    issue_type: str  # e.g., "pod_crashloop", "oom", "image_pull_backoff", "quota_exceeded", "pod_failure"
    namespace: str
    resource: Dict[str, str]  # {"kind": "Pod", "name": "pod-name"}
    event_reason: Optional[str] = None
    issue_details: Dict[str, Any] = {}  # Issue-specific details (container_name, quota_details, etc.)
    remediation_strategy: str = "auto"

class RemediationResponse(BaseModel):
    """Response from remediation execution"""
    status: str
    message: str
    details: Optional[Dict[str, Any]] = None  


class MCPDebugLogger:
    """Helper class for logging and handling MCP tool call debugging"""
    
    def __init__(self):
        """Initialize the debug logger with MCP_TOOL_LOGGING environment variable"""
        self.enable_logging = os.getenv("MCP_TOOL_LOGGING", "false").lower() == "true"
    
    def parse_event_type_and_payload(self, event):
        """Extract event type and payload from event object"""
        event_type = None
        payload = None
        
        if hasattr(event, 'event_type'):
            event_type = event.event_type
            payload = getattr(event, 'payload', None)
        elif hasattr(event, 'payload') and hasattr(event.payload, 'event_type'):
            event_type = event.payload.event_type
            payload = event.payload
        else:
            # Event might be the payload itself - check for common event types
            event_type = type(event).__name__
            # Skip TurnStarted events
            if event_type == "TurnStarted":
                return None, None
            payload = event
        
        return event_type, payload
    
    def extract_tool_response_content(self, content):
        """Extract and format tool response content from various formats"""
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if hasattr(item, 'text'):
                    text_parts.append(item.text)
                elif isinstance(item, str):
                    text_parts.append(item)
            return '\n'.join(text_parts) if text_parts else str(content)
        elif hasattr(content, 'text'):
            return content.text
        return content
    
    def format_content_for_display(self, content):
        """Format content for display, attempting JSON parsing if possible"""
        try:
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    return json.dumps(parsed, indent=2)
                except:
                    return content
            else:
                return json.dumps(content, indent=2)
        except:
            return str(content)
    
    def print_tool_call(self, tool_name, call_id, arguments, execution_num=None, source=None):
        """Print tool call information (only if logging enabled)"""
        if not self.enable_logging:
            return
        
        print(f"\n{'='*60}")
        if execution_num:
            print(f"🔧 TOOL EXECUTION #{execution_num}")
        elif source:
            print(f"🔧 TOOL EXECUTION ({source})")
        else:
            print(f"🔧 TOOL EXECUTION")
        print(f"{'='*60}")
        print(f"\n📞 MCP CALL:")
        print(f"   Tool: {tool_name}")
        if call_id:
            print(f"   Call ID: {call_id}")
        if arguments and arguments != {}:
            print(f"   Arguments: {json.dumps(arguments, indent=2)}")
        else:
            print(f"   Arguments: (none)")
    
    def print_tool_response(self, tool_name, call_id, content, source=None):
        """Print tool response information (only if logging enabled)"""
        if not self.enable_logging:
            return
        
        print(f"\n MCP RESPONSE" + (f" ({source})" if source else "") + ":")
        print(f"   Tool: {tool_name}")
        if call_id:
            print(f"   Call ID: {call_id}")
        print(self.format_content_for_display(content))
        print(f"{'='*60}\n")
    
    def handle_step_start(self, event, payload, tool_executions_list):
        """Handle step_start events that might contain tool call info"""
        if not payload:
            return
        
        step_type = getattr(payload, 'step_type', None)
        if step_type == "tool_execution":
            # Check for tool_calls in step_start
            if hasattr(payload, 'tool_calls'):
                for tool_call in payload.tool_calls:
                    tool_name = getattr(tool_call, 'tool_name', 'unknown')
                    arguments = getattr(tool_call, 'arguments', {})
                    
                    # Log raw arguments for debugging JSON issues
                    arguments_raw = arguments
                    if isinstance(arguments, str):
                        logger.debug(f"Tool '{tool_name}' arguments (raw string from step_start): {repr(arguments)}")
                    elif hasattr(tool_call, '__dict__'):
                        logger.debug(f"Tool '{tool_name}' tool_call object: {tool_call.__dict__}")
                    
                    self.print_tool_call(tool_name, None, arguments, source="step_start")
    
    def handle_output_item(self, event, payload, tool_executions_list):
        """Handle output_item events which contain tool execution results"""
        if self.enable_logging:
            event_type = getattr(event, 'event_type', type(event).__name__)
            logger.debug(f"Found potential tool response event: {event_type}")
        
        # Try to extract tool response from various locations
        item = None
        if payload:
            item = getattr(payload, 'item', None) or getattr(payload, 'tool_call', None)
        if not item and hasattr(event, 'item'):
            item = event.item
        
        if item:
            # Check if this is an MCP call result
            item_type = str(getattr(item, 'type', '')).lower()
            if 'mcp' in item_type or hasattr(item, 'tool_name'):
                # Extract tool response from the item
                content = None
                if hasattr(item, 'content'):
                    content = item.content
                elif hasattr(item, 'tool_call_log'):
                    content = item.tool_call_log
                elif hasattr(item, 'text'):
                    content = item.text
                
                tool_name = getattr(item, 'tool_name', 'unknown')
                call_id = getattr(item, 'call_id', None) or getattr(item, 'id', None)
                
                if content:
                    formatted_content = self.extract_tool_response_content(content)
                    self.print_tool_response(tool_name, call_id, formatted_content)
                    
                    tool_executions_list.append({
                        'tool_name': tool_name,
                        'response': formatted_content
                    })
    
    def handle_step_progress(self, event, payload, final_text, tool_calls_made, tool_execution_count):
        """Handle step_progress events for tool calls and text deltas"""
        delta = getattr(payload, 'delta', None) if payload else None
        if not delta and hasattr(event, 'delta'):
            delta = event.delta
        
        if delta:
            if hasattr(delta, 'delta_type') and delta.delta_type == "tool_call_issued":
                tool_execution_count += 1
                
                # Try to extract tool call info from delta
                tool_name = getattr(delta, 'tool_name', None)
                call_id = getattr(delta, 'call_id', None)
                arguments = getattr(delta, 'arguments', None)
                
                # Log raw arguments if available (for JSON debugging)
                if arguments is not None and isinstance(arguments, str):
                    logger.debug(f"Tool call issued - {tool_name} (call_id: {call_id})")
                    logger.debug(f"  Raw arguments from delta: {repr(arguments)}")
                
                tool_calls_made.append(delta)
                # Don't print here - wait for step_complete which has arguments
            
            # Print text delta (always show agent text output)
            elif hasattr(delta, 'text'):
                text = delta.text
                final_text += text
                print(text, end='', flush=True)
        
        return tool_execution_count, final_text
    
    def handle_step_complete(self, event, tool_calls_made, printed_tool_calls, tool_executions_list):
        """Handle step_complete events for tool execution results"""
        step_type = getattr(event, 'step_type', None)
        result = getattr(event, 'result', None) if hasattr(event, 'result') else None
        
        tool_responses = None
        tool_calls_with_args = None
        
        # For tool_execution steps, get tool_calls (which have the actual arguments!)
        if step_type == "tool_execution" and result:
            # Get tool_calls which contain the actual arguments
            if hasattr(result, 'tool_calls'):
                tool_calls_with_args = result.tool_calls
            
            # Get tool_responses
            if hasattr(result, 'tool_responses'):
                tool_responses = result.tool_responses
        
        # For inference steps, check server_tool_executions (might contain responses)
        elif step_type == "inference" and result:
            if hasattr(result, 'server_tool_executions') and result.server_tool_executions:
                # server_tool_executions might contain the actual tool responses
                for exec_result in result.server_tool_executions:
                    if hasattr(exec_result, 'content') or hasattr(exec_result, 'tool_response'):
                        content = getattr(exec_result, 'content', None) or getattr(exec_result, 'tool_response', None)
                        tool_name = getattr(exec_result, 'tool_name', 'unknown')
                        call_id = getattr(exec_result, 'call_id', None)
                        
                        if content:
                            formatted_content = self.extract_tool_response_content(content)
                            self.print_tool_response(tool_name, call_id, formatted_content, source="server_tool_executions")
                            
                            tool_executions_list.append({
                                'tool_name': tool_name,
                                'response': formatted_content
                            })
        
        # Print tool calls with arguments (for tool_execution steps)
        if tool_calls_with_args and step_type == "tool_execution":
            for tool_call in tool_calls_with_args:
                tool_name = getattr(tool_call, 'tool_name', 'unknown')
                call_id = getattr(tool_call, 'call_id', None)
                arguments_str = getattr(tool_call, 'arguments', '{}')
                
                # Parse arguments with detailed error logging
                try:
                    if isinstance(arguments_str, str):
                        # Log raw JSON before parsing (in debug mode, or if string looks suspicious)
                        if self.enable_logging or len(arguments_str) > 0:
                            logger.debug(f"Parsing JSON for tool '{tool_name}' (call_id: {call_id}):")
                            logger.debug(f"  Raw arguments string (length: {len(arguments_str)}): {repr(arguments_str)}")
                        
                        try:
                            arguments = json.loads(arguments_str)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON parsing failed for tool '{tool_name}': {e}")
                            logger.error(f"Attempting to fix common JSON issues...")
                            
                            # Try to fix common issues
                            try:
                                # Remove trailing commas
                                fixed_str = re.sub(r',\s*}', '}', arguments_str)
                                fixed_str = re.sub(r',\s*]', ']', fixed_str)
                                arguments = json.loads(fixed_str)
                                logger.info("✓ Fixed JSON by removing trailing commas")
                            except:
                                # If still fails, try to extract just the valid part
                                logger.warning("Could not fix JSON, using empty dict")
                                arguments = {}
                    else:
                        arguments = arguments_str
                except json.JSONDecodeError as e:
                    # Log detailed JSON parsing error (always log, not just in debug mode)
                    error_msg = f" JSON PARSING ERROR for tool '{tool_name}' (call_id: {call_id})"
                    logger.error("=" * 60)
                    logger.error(error_msg)
                    logger.error(f"  Error: {str(e)}")
                    logger.error(f"  Error at line {e.lineno}, column {e.colno} (position {e.pos})")
                    logger.error(f"  Arguments string type: {type(arguments_str)}")
                    logger.error(f"  Arguments string length: {len(arguments_str) if isinstance(arguments_str, str) else 'N/A'}")
                    logger.error(f"  Raw arguments string (first 500 chars): {repr(arguments_str[:500]) if isinstance(arguments_str, str) else arguments_str}")
                    
                    # Always log full string for JSON errors (they're critical)
                    if isinstance(arguments_str, str):
                        logger.error(f"  Full arguments string: {repr(arguments_str)}")
                        
                        # Try to show context around the error
                        if e.pos is not None and len(arguments_str) > e.pos:
                            start = max(0, e.pos - 50)
                            end = min(len(arguments_str), e.pos + 50)
                            context = arguments_str[start:end]
                            marker_pos = e.pos - start
                            logger.error(f"  Context around error position ({e.pos}):")
                            logger.error(f"    ...{context}...")
                            logger.error(f"    {' ' * (marker_pos + 3)}^ (error here)")
                            
                            # Try to identify the problematic character
                            if e.pos < len(arguments_str):
                                problem_char = arguments_str[e.pos]
                                logger.error(f"  Problematic character at position {e.pos}: {repr(problem_char)} (ord: {ord(problem_char)})")
                    
                    logger.error("=" * 60)
                    
                    # Use empty dict as fallback
                    arguments = {}
                except Exception as e:
                    logger.error(f" Unexpected error parsing arguments for tool '{tool_name}': {type(e).__name__}: {str(e)}")
                    logger.error(f"  Arguments type: {type(arguments_str)}, value: {repr(arguments_str)[:200]}")
                    arguments = arguments_str if arguments_str else {}
                
                # Find matching tool execution number
                matching_exec_num = None
                for idx, prev_call in enumerate(tool_calls_made, 1):
                    if getattr(prev_call, 'call_id', None) == call_id:
                        matching_exec_num = idx
                        break
                
                # Print if we haven't printed this call_id yet
                if call_id not in printed_tool_calls and matching_exec_num:
                    self.print_tool_call(tool_name, call_id, arguments, execution_num=matching_exec_num)
                    if call_id:
                        printed_tool_calls.add(call_id)
        
        # Print tool responses
        if tool_responses and len(tool_responses) > 0:
            for tool_response in tool_responses:
                content = getattr(tool_response, 'content', '')
                tool_name = getattr(tool_response, 'tool_name', 'unknown')
                call_id = getattr(tool_response, 'call_id', 'N/A')
                
                formatted_content = self.extract_tool_response_content(content)
                self.print_tool_response(tool_name, call_id, formatted_content)
                
                tool_executions_list.append({
                    'tool_name': tool_name,
                    'response': formatted_content
                })
    
    def handle_turn_completed(self, event, payload, final_text, tool_executions_list):
        """Handle turn_completed events to extract final output and tool responses"""
        # Get final output and check for tool responses in the turn
        turn = None
        if payload and hasattr(payload, 'turn'):
            turn = payload.turn
        elif hasattr(event, 'turn'):
            turn = event.turn
        elif hasattr(event, 'payload') and hasattr(event.payload, 'turn'):
            turn = event.payload.turn
        
        if turn:
            # Get final output
            if hasattr(turn, 'output_message'):
                output = turn.output_message.content
                if output and not final_text:
                    final_text = str(output)
            
            # Check for tool responses in turn.steps
            if hasattr(turn, 'steps'):
                for step in turn.steps:
                    if getattr(step, 'step_type', None) == "tool_execution":
                        if hasattr(step, 'result') and hasattr(step.result, 'tool_responses'):
                            tool_responses = step.result.tool_responses
                            if tool_responses:
                                for tool_response in tool_responses:
                                    content = getattr(tool_response, 'content', '')
                                    tool_name = getattr(tool_response, 'tool_name', 'unknown')
                                    call_id = getattr(tool_response, 'call_id', 'N/A')
                                    
                                    formatted_content = self.extract_tool_response_content(content)
                                    self.print_tool_response(tool_name, call_id, formatted_content)
                                    
                                    tool_executions_list.append({
                                        'tool_name': tool_name,
                                        'response': formatted_content
                                    })
        
        return final_text
    
    def log_tool_call_details(self, tool_call, source):
        """Log detailed information about a tool call object"""
        try:
            tool_name = getattr(tool_call, 'tool_name', 'unknown')
            call_id = getattr(tool_call, 'call_id', None)
            arguments = getattr(tool_call, 'arguments', None)
            
            logger.error(f"      Tool Call from {source}:")
            logger.error(f"        tool_name: {tool_name}")
            logger.error(f"        call_id: {call_id}")
            
            # Log arguments in all possible formats
            if arguments is not None:
                logger.error(f"        arguments type: {type(arguments).__name__}")
                
                if isinstance(arguments, str):
                    logger.error(f"        arguments (raw string): {repr(arguments)}")
                    logger.error(f"        arguments (string length): {len(arguments)}")
                    # Try to show where the error might be
                    if len(arguments) > 100:
                        logger.error(f"        arguments (first 100 chars): {repr(arguments[:100])}")
                        logger.error(f"        arguments (last 100 chars): {repr(arguments[-100:])}")
                elif isinstance(arguments, dict):
                    logger.error(f"        arguments (dict): {json.dumps(arguments, indent=10)}")
                else:
                    logger.error(f"        arguments (repr): {repr(arguments)}")
                    logger.error(f"        arguments (str): {str(arguments)}")
            else:
                logger.error(f"        arguments: None")
            
            # Log all attributes of the tool_call object
            if self.enable_logging:
                attrs = [a for a in dir(tool_call) if not a.startswith('_')]
                logger.error(f"        tool_call attributes: {attrs}")
                for attr in attrs:
                    try:
                        value = getattr(tool_call, attr)
                        if not callable(value):
                            logger.error(f"          {attr}: {type(value).__name__} = {repr(str(value)[:100])}")
                    except:
                        pass
        except Exception as e:
            logger.error(f"      Error logging tool call details: {e}")
            logger.error(f"      tool_call object: {repr(tool_call)}")
    
    def handle_turn_failed(self, event, payload, stream_error, tool_calls_made=None, printed_tool_calls=None):
        """Handle turn_failed events (always show errors)"""
        print(f"\n{'='*60}")
        print(f" TURN FAILED")
        print(f"{'='*60}")
        
        # Extract error message
        error_message = None
        error_details = None
        
        if payload and hasattr(payload, 'error_message'):
            error_message = payload.error_message
            if hasattr(payload, 'error'):
                error_details = payload.error
        elif hasattr(event, 'error_message'):
            error_message = event.error_message
        elif hasattr(event, 'turn') and hasattr(event.turn, 'error_message'):
            error_message = event.turn.error_message
        elif hasattr(event, 'error'):
            error_message = str(event.error)
            error_details = event.error
        
        if error_message:
            print(f"Error Message: {error_message}")
            
            # Check if error message contains JSON parsing hints
            if "Expecting" in error_message or "JSON" in error_message or "delimiter" in error_message or "value" in error_message:
                logger.error("=" * 60)
                logger.error(f" JSON PARSING ERROR DETECTED: {error_message}")
                logger.error("=" * 60)
                
                # FIRST: Check tool_calls_made for incomplete tool calls (most likely source of the error)
                if tool_calls_made and printed_tool_calls is not None:
                    incomplete_calls = []
                    for delta in tool_calls_made:
                        call_id = getattr(delta, 'call_id', None)
                        if call_id and call_id not in printed_tool_calls:
                            incomplete_calls.append(delta)
                    
                    if incomplete_calls:
                        logger.error(f"\n🔍 Found {len(incomplete_calls)} incomplete tool call(s) that likely caused the JSON parsing error:")
                        for idx, delta in enumerate(incomplete_calls, 1):
                            logger.error(f"\n  Incomplete Tool Call #{idx}:")
                            logger.error(f"    Delta type: {type(delta).__name__}")
                            logger.error(f"    Delta attributes: {[a for a in dir(delta) if not a.startswith('_')]}")
                            
                            # Extract all possible argument locations
                            tool_name = getattr(delta, 'tool_name', None)
                            call_id = getattr(delta, 'call_id', None)
                            arguments = getattr(delta, 'arguments', None)
                            
                            logger.error(f"    tool_name: {tool_name}")
                            logger.error(f"    call_id: {call_id}")
                            
                            if arguments is not None:
                                logger.error(f"     ARGUMENTS FOUND (type: {type(arguments).__name__}):")
                                if isinstance(arguments, str):
                                    logger.error(f"      Raw arguments string: {repr(arguments)}")
                                    logger.error(f"      Arguments string length: {len(arguments)}")
                                    
                                    # Extract error position from error message
                                    try:
                                        error_pos = int(error_message.split('char ')[1].split(')')[0]) if 'char ' in error_message else None
                                        if error_pos:
                                            logger.error(f"      ERROR POSITION MISMATCH:")
                                            logger.error(f"         Error at position: {error_pos}")
                                            logger.error(f"         Arguments length: {len(arguments)}")
                                            if error_pos >= len(arguments):
                                                logger.error(f"         Error position ({error_pos}) is BEYOND arguments length ({len(arguments)})!")
                                                logger.error(f"         This means the error is in a LARGER JSON structure that includes these arguments.")
                                                logger.error(f"         The full request payload is likely: {{'tool': '{tool_name}', 'arguments': {arguments}, 'call_id': '{call_id}', ...}}")
                                                logger.error(f"         The error is at position {error_pos} in that FULL payload, not just in arguments.")
                                    except Exception as e:
                                        logger.error(f"      Could not extract error position: {e}")
                                    
                                    # Show context around the error position if it's within arguments
                                    try:
                                        error_pos = int(error_message.split('char ')[1].split(')')[0]) if 'char ' in error_message else None
                                        if error_pos and error_pos < len(arguments):
                                            start = max(0, error_pos - 50)
                                            end = min(len(arguments), error_pos + 50)
                                            context = arguments[start:end]
                                            marker_pos = error_pos - start
                                            logger.error(f"      Context around error position ({error_pos}):")
                                            logger.error(f"        ...{context}...")
                                            logger.error(f"        {' ' * (marker_pos + 3)}^ (error here)")
                                    except:
                                        pass
                                elif isinstance(arguments, dict):
                                    logger.error(f"      Arguments dict: {json.dumps(arguments, indent=8)}")
                                else:
                                    logger.error(f"      Arguments (repr): {repr(arguments)}")
                            else:
                                logger.error(f"    No arguments found in delta object")
                            
                            # Log ALL delta attributes to see the full structure
                            logger.error(f"    Full delta object dump:")
                            try:
                                # Try to serialize the entire delta object
                                delta_dict = {}
                                for attr in dir(delta):
                                    if not attr.startswith('_'):
                                        try:
                                            value = getattr(delta, attr)
                                            if not callable(value):
                                                delta_dict[attr] = value
                                        except:
                                            pass
                                
                                # Log as JSON if possible
                                try:
                                    logger.error(f"      Delta as dict: {json.dumps(delta_dict, indent=10, default=str)}")
                                except:
                                    logger.error(f"      Delta attributes: {delta_dict}")
                                
                                # Also try __dict__ if available
                                if hasattr(delta, '__dict__'):
                                    logger.error(f"      Delta __dict__: {delta.__dict__}")
                                    
                            except Exception as e:
                                logger.error(f"      Error dumping delta: {e}")
                            
                            # Try to get arguments from other delta attributes
                            for attr in dir(delta):
                                if not attr.startswith('_'):
                                    try:
                                        value = getattr(delta, attr)
                                        if value is not None and not callable(value):
                                            value_str = str(value)
                                            # Only log if it's potentially relevant (contains JSON-like content or is long)
                                            if len(value_str) > 10 or '{' in value_str or '[' in value_str:
                                                logger.error(f"    {attr}: {type(value).__name__} = {repr(value_str[:500])}")
                                    except:
                                        pass
                    
                    if not incomplete_calls:
                        logger.error("\n No incomplete tool calls found in tool_calls_made list")
                
                # SECOND: Try to get the turn and its steps (fallback if tool_calls_made doesn't have the data)
                logger.error("=" * 60)
                logger.error(f" JSON PARSING ERROR DETECTED: {error_message}")
                logger.error("=" * 60)
                
                # Try to get the turn and its steps to find the problematic tool call
                turn = None
                if payload and hasattr(payload, 'turn'):
                    turn = payload.turn
                elif hasattr(event, 'turn'):
                    turn = event.turn
                elif hasattr(event, 'payload') and hasattr(event.payload, 'turn'):
                    turn = event.payload.turn
                
                if turn:
                    logger.error(f"Turn object found: {type(turn).__name__}")
                    logger.error(f"Turn attributes: {[a for a in dir(turn) if not a.startswith('_')]}")
                    
                    # Try to get steps
                    steps = None
                    if hasattr(turn, 'steps'):
                        steps = turn.steps
                    elif hasattr(turn, 'step'):
                        steps = [turn.step] if turn.step else []
                    
                    if steps:
                        logger.error(f"Extracting tool calls from {len(steps)} step(s):")
                        for idx, step in enumerate(steps):
                            step_type = getattr(step, 'step_type', 'unknown')
                            logger.error(f"\n  Step {idx}: {step_type}")
                            
                            # Check step.input for tool calls
                            if hasattr(step, 'input'):
                                input_obj = step.input
                                logger.error(f"    Step input type: {type(input_obj).__name__}")
                                if hasattr(input_obj, 'tool_calls'):
                                    for tc in input_obj.tool_calls:
                                        self.log_tool_call_details(tc, "step.input")
                            
                            # Check step.result for tool calls
                            if hasattr(step, 'result'):
                                result = step.result
                                logger.error(f"    Step result type: {type(result).__name__}")
                                
                                # Check result.tool_calls
                                if hasattr(result, 'tool_calls'):
                                    logger.error(f"    Found {len(result.tool_calls)} tool calls in result.tool_calls:")
                                    for tc in result.tool_calls:
                                        self.log_tool_call_details(tc, "step.result.tool_calls")
                                
                                # Check result.tool_call (singular)
                                if hasattr(result, 'tool_call'):
                                    self.log_tool_call_details(result.tool_call, "step.result.tool_call")
                                
                                # Check result.tool_call_log
                                if hasattr(result, 'tool_call_log'):
                                    logger.error(f"    tool_call_log: {repr(result.tool_call_log)}")
                            
                            # Check step directly for tool_calls
                            if hasattr(step, 'tool_calls'):
                                logger.error(f"    Found {len(step.tool_calls)} tool calls directly on step:")
                                for tc in step.tool_calls:
                                    self.log_tool_call_details(tc, "step.tool_calls")
                            
                            # Log all step attributes (always log for JSON errors)
                            step_attrs = [a for a in dir(step) if not a.startswith('_')]
                            logger.error(f"    Step attributes: {step_attrs}")
                            
                            # Try to serialize step to see its full structure
                            try:
                                step_dict = step.__dict__ if hasattr(step, '__dict__') else {}
                                logger.error(f"    Step __dict__ keys: {list(step_dict.keys())}")
                                for key, value in step_dict.items():
                                    if 'tool' in key.lower() or 'call' in key.lower() or 'arg' in key.lower():
                                        logger.error(f"      {key}: {type(value).__name__} = {repr(str(value)[:200])}")
                            except:
                                pass
                    else:
                        logger.error("  No steps found in turn")
                else:
                    logger.error("  No turn object found in event/payload")
                    logger.error(f"  Event type: {type(event).__name__}")
                    logger.error(f"  Event attributes: {[a for a in dir(event) if not a.startswith('_')]}")
                    if payload:
                        logger.error(f"  Payload type: {type(payload).__name__}")
                        logger.error(f"  Payload attributes: {[a for a in dir(payload) if not a.startswith('_')]}")
                
                # Also check event and payload directly
                logger.error("\nChecking event and payload for tool calls:")
                if hasattr(event, 'tool_calls'):
                    logger.error(f"  Event has tool_calls: {len(event.tool_calls)}")
                    for tc in event.tool_calls:
                        self.log_tool_call_details(tc, "event.tool_calls")
                
                if payload and hasattr(payload, 'tool_calls'):
                    logger.error(f"  Payload has tool_calls: {len(payload.tool_calls)}")
                    for tc in payload.tool_calls:
                        self.log_tool_call_details(tc, "payload.tool_calls")
                
                logger.error("=" * 60)
        else:
            # Print the whole event for debugging
            print(f"Event details: {event}")
            if self.enable_logging:
                print(f"Event attributes: {[a for a in dir(event) if not a.startswith('_')]}")
                logger.error(f"Full event object: {event}")
                logger.error(f"Payload: {payload}")
        
        print(f"{'='*60}\n")
        
        # Store error for diagnostics
        if not stream_error:
            stream_error = RuntimeError(f"Turn failed: {error_message or 'Unknown error'}")
        
        return stream_error
    
    def print_stream_termination_diagnostics(self, chunk_count, stream_duration, turn_completed, 
                                             last_event_type, last_event_class, stream_error, 
                                             incomplete_calls, tool_calls_made, printed_tool_calls):
        """Print stream termination diagnostics (only if logging enabled)"""
        if not self.enable_logging:
            return
        
        print(f"\n{'='*60}")
        print(f"  STREAM TERMINATION DIAGNOSTICS")
        print(f"{'='*60}")
        print(f"Total chunks processed: {chunk_count}")
        print(f"Stream duration: {stream_duration:.2f}s")
        print(f"Turn completed: {turn_completed}")
        print(f"Last event: {last_event_type} ({last_event_class})")
        if stream_error:
            print(f"Error type: {type(stream_error).__name__}")
            print(f"Error message: {str(stream_error)}")
        print(f"Incomplete tool calls: {len(incomplete_calls)}")
        print(f"{'='*60}\n")
        
        for idx, tool_call_delta in enumerate(incomplete_calls, 1):
            call_id = getattr(tool_call_delta, 'call_id', None)
            tool_name = getattr(tool_call_delta, 'tool_name', 'unknown')
            print(f"\n{'='*60}")
            print(f"🔧 TOOL EXECUTION #{len(printed_tool_calls) + idx}")
            print(f"{'='*60}")
            print(f"\n📞 MCP CALL:")
            print(f"   Tool: {tool_name}")
            print(f"   Call ID: {call_id}")
            print(f"   Arguments: (not available - stream ended before step_complete event)")
            print(f"   Diagnostic: tool_call_issued event received but step_complete event not received")
            if stream_error:
                print(f"   Error: {type(stream_error).__name__}: {str(stream_error)}")
            printed_tool_calls.add(call_id)


class OCPMCPRemediationAgent:  
    """OCP MCP Remediation Agent for automated Kubernetes issue remediation"""  
      
    def __init__(self, debug: bool = False):  
        self.llama_stack_url = os.getenv("LLAMA_STACK_URL", "https://lls-llamastack.apps.cluster-qgrcw.qgrcw.sandbox421.opentlc.com")  
        self.ocp_mcp_endpoint = os.getenv("OCP_MCP_ENDPOINT",   
            "https://ocp-mcp-llamastack.apps.cluster-qgrcw.qgrcw.sandbox421.opentlc.com/mcp")  
        # Set timeout for streaming responses (default 30s, increase for tool execution)
        self.client_timeout = int(os.getenv("LLAMA_STACK_CLIENT_TIMEOUT", "600"))  # 10 minutes
        self.client = None  
        self.model_id = None  
        self.debug = debug
        self.debug_logger = MCPDebugLogger()

    def _generate_instructions(self, request: RemediationRequest) -> str:
            """Generate autonomous instructions for any SRE incident."""
            namespace = request.namespace
            resource_kind = request.resource.get("kind", "Unknown")
            resource_name = request.resource.get("name", "Unknown")
            event_reason = request.event_reason or "Unknown"
            issue_type = request.issue_type
            
            return f"""You are a **Senior Site Reliability Engineer (Level 2)** for an OpenShift/Kubernetes cluster.
    Your goal is to autonomously diagnose and fix the incident described below.

    ### INCIDENT CONTEXT
    * **Issue Type:** {issue_type}
    * **Trigger Event:** "{event_reason}"
    * **Target Resource:** {resource_kind}/{resource_name}
    * **Namespace:** {namespace}

    ### STANDARD OPERATING PROCEDURE (SOP)
    You must follow this 4-step loop. Do not skip steps.

    **PHASE 1: INVESTIGATE (Information Gathering)**
    * **Objective:** Find the technical root cause (e.g., "Memory limit 128Mi exceeded", "Secret not found").
    * **Tools:** 1. Run `pods_get` (or `resources_get`) to inspect `status.containerStatuses` or `status.conditions`.
        2. Run `events_list` (filtered by namespace) to see scheduler errors.
        3. Run `pods_log` (tail=50) ONLY if the pod is running/crashing. 
    * **Constraint:** Trust the resource names provided. Do not "search" the whole cluster.

    **PHASE 2: TRIAGE & PLAN**
    * If **Transient** (Network blip) -> **Plan:** Restart/Delete Pod.
    * If **Capacity** (OOM, Quota) -> **Plan:** Clear wasted resources (zombie pods) first. If that fails, calculate the required increase and patch the limit.
    * If **Configuration** (Immutable Field Error) -> **Plan:** Delete and Recreate the resource (See Tooling Tactics below).

    **PHASE 3: EXECUTE (Remediation)**
    * Run the necessary tool (`pods_delete`, `resources_create_or_update`, `resources_scale`).
    * **Safety Rule:** When editing Quotas/Limits, calculate the *exact* need + 20% buffer. Do not guess.

    **PHASE 4: VERIFY**
    * Check the resource status one last time.
    * **Final Output:** Emit a message summarizing: "Root Cause: [X]. Action Taken: [Y]. Current Status: [Z]."

    ### TOOLING TACTICS & WORKAROUNDS
    1. **NO FORCE UPDATE:** The tool `resources_create_or_update` does **NOT** support `--force`.
    - If you need to update a resource but get a "Conflict" or "Immutable Field" error, you must **DELETE** the resource first using `resources_delete`, then recreate it.
    - **EXCEPTION:** Never delete a PersistentVolumeClaim (PVC) or Service (ClusterIP) to update it unless absolutely necessary, as this destroys data/IPs.
    2. **DATA HYGIENE:** When fetching logs, **ALWAYS** use `tail=50`.

    ### 🛡️ OPERATIONAL GUARDRAILS
    1. **SCOPE LOCK:** Work **ONLY** in namespace `{namespace}`.
    2. **DESTRUCTIVE ACTION:** Deleting a Pod is a standard L1 fix (Safe). Deleting a Deployment/ResourceQuota is L2 (Safe if config is known). Deleting Storage is L3 (UNSAFE - Avoid).
    """        

      
    def register_ocp_mcp_server(self):  
        logger.info("=" * 60)  
        logger.info("STEP 1: Registering OCP MCP Server")  
        logger.info("=" * 60)  
          
        # Initialize client with extended timeout for tool execution
        self.client = LlamaStackClient(base_url=self.llama_stack_url, timeout=self.client_timeout)  
        logger.info(f"Initialized LlamaStackClient with URL: {self.llama_stack_url}")
        logger.info(f"Client timeout set to: {self.client_timeout} seconds")  
          
        # Get available models  
        models = self.client.models.list()  
        self.model_id = os.getenv("MODEL_ID", "gpt-5.2")  
        logger.info(f"Selected model: {self.model_id}")  
                  
        # Register OCP MCP toolgroup  
        try:  
            self.client.toolgroups.register(  
                toolgroup_id="mcp::ocp-mcp",  
                provider_id="model-context-protocol",  
                mcp_endpoint={"uri": self.ocp_mcp_endpoint},  
            )  
            logger.info("✓ Successfully registered OCP MCP toolgroup")  
        except Exception as e:  
            logger.info(f"Toolgroup registration note (may already exist): {e}")  
          
        # List and verify available tools  
        try:  
            tools = self.client.tools.list(toolgroup_id="mcp::ocp-mcp")  
            tool_identifiers = [t.name for t in tools]  
            logger.info(f" Available OCP MCP tools ({len(tool_identifiers)}):")  
            for tool in tool_identifiers:  
                logger.info(f"  - {tool}")  
        except Exception as e:  
            logger.error(f" Error listing tools: {e}")  
            raise  
          
        logger.info("=" * 60)  
        logger.info("STEP 1 COMPLETE: OCP MCP Server Registered")  
        logger.info("=" * 60)  
        return True  
      
    def execute_remediation(self, request: RemediationRequest):  
        """Execute remediation for the given structured request"""  
        logger.info("=" * 60)  
        logger.info("EXECUTING REMEDIATION")  
        logger.info("=" * 60)  
        logger.info(f"Issue Type: {request.issue_type}")  
        logger.info(f"Namespace: {request.namespace}")  
        logger.info(f"Resource: {request.resource}")  
        logger.info(f"Event Reason: {request.event_reason}")  
        logger.info(f"Issue Details: {request.issue_details}")  
        logger.info("=" * 60)  
          
        if not self.client or not self.model_id:  
            raise RuntimeError("Client not initialized. Run register_ocp_mcp_server() first")  
          
        # Generate dynamic instructions based on issue type
        instructions = self._generate_instructions(request)
          
        # Create agent with instructions  
        tools_list = self.client.tool_runtime.list_tools(tool_group_id="mcp::ocp-mcp")   

        RESTRICTED_TOOLS = [
                    "configuration_view", 
                    "resources_list",      # This usually dumps ALL resources in the cluster       # Can be huge if not namespaced
                    "namespaces_list",
                    "helm_install",
                    "helm_list",
                    "helm_uninstall",
                    "nodes_log",
                    "nodes_stats_summary",
                    "nodes_top",
                    "projects_list",
                    "resources_scale", 
                    "pods_get",
                    "pods_list",
                    "pods_list_in_namespace",
                ]
        
        safe_tool_names = [
            t.name for t in tools_list 
            if t.name not in RESTRICTED_TOOLS
        ]

        tool_defs = [    
            {    
                "type": "mcp",    
                "server_url": self.ocp_mcp_endpoint,    
                "server_label": "mcp::ocp-mcp",    
                "require_approval": "never",    
                "allowed_tools": safe_tool_names,  
            }    
        ]  

        agent = Agent(  
            self.client,  
            model=self.model_id,  
            instructions=instructions,  
            tools=tool_defs
        )
        logger.info("✓ Created agent with OCP MCP tools")
          
        # Create session  
        session_id = agent.create_session(session_name=f"remediation-{uuid.uuid4().hex}")  
        logger.info(f"✓ Created session: {session_id}")  
          
        # Build structured prompt with all context
        resource_kind = request.resource.get("kind", "Unknown")
        resource_name = request.resource.get("name", "Unknown")
        prompt = f"Remediate the {request.issue_type} issue for {resource_kind}/{resource_name} in namespace {request.namespace}."
        if request.resource.get("uid"):
            prompt += f" Resource UID: {request.resource.get('uid')}."
        logger.info(f"Executing agent with prompt: {prompt}")  
        logger.info("=" * 60)  
          
        try:  
            # Track execution (llama-stack-client 0.3.1 API)
            tool_executions_list = []
            tool_execution_count = 0
              
            logger.info("Agent execution started...")  
            logger.info("-" * 60)  
              
            # Create agent turn with streaming  
            response = agent.create_turn(  
                messages=[{"role": "user", "content": prompt}],  
                session_id=session_id,  
                stream=True,  
            )  
            
            # Process streaming response - 0.3.1 API
            # According to deepwiki: stream should continue until turn_completed event
            final_text = ""
            tool_calls_made = []
            printed_tool_calls = set()  # Track which call_ids we've already printed
            turn_completed = False
            chunk_count = 0
            last_chunk_time = time.time()
            last_event_type = None
            last_event_class = None
            stream_error = None
            stream_start_time = time.time()
            
            try:  
                # Fully consume stream until turn_completed (per deepwiki guidance)
                for chunk in response:
                    chunk_count += 1
                    current_time = time.time()
                    time_since_last = current_time - last_chunk_time
                    if time_since_last > 5 and self.debug:  # Log if gap > 5 seconds
                        logger.debug(f"Gap of {time_since_last:.2f}s since last chunk (chunk #{chunk_count})")
                    last_chunk_time = current_time
                    
                    # Handle different event structures
                    event = chunk.event
                    
                    # Parse event type and payload
                    event_type, payload = self.debug_logger.parse_event_type_and_payload(event)
                    if event_type is None:  # Skip TurnStarted events
                        continue
                    
                    # Track last event for diagnostics
                    last_event_type = event_type
                    last_event_class = type(event).__name__
                    
                    # Handle step_start events (might have tool call info)
                    if event_type == "step_start" or event_type == "StepStarted":
                        self.debug_logger.handle_step_start(event, payload, tool_executions_list)
                    
                    # Check for output_item events which contain tool execution results
                    elif event_type and ('output_item' in event_type.lower() or 'mcp_call' in event_type.lower()):
                        self.debug_logger.handle_output_item(event, payload, tool_executions_list)
                    
                    # Handle step_progress events
                    elif event_type == "step_progress" or (payload and hasattr(payload, 'delta')):
                        tool_execution_count, final_text = self.debug_logger.handle_step_progress(
                            event, payload, final_text, tool_calls_made, tool_execution_count
                        )
                    
                    # Handle step_complete events
                    elif event_type in ["step_complete", "step_completed", "StepCompleted"] or type(event).__name__ == "StepCompleted":
                        self.debug_logger.handle_step_complete(event, tool_calls_made, printed_tool_calls, tool_executions_list)
                    
                    # Handle turn_completed events
                    elif event_type in ["turn_completed", "TurnCompleted"] or type(event).__name__ == "TurnCompleted":
                        turn_completed = True
                        if self.debug:
                            logger.debug(f"Received turn_completed event after {chunk_count} chunks")
                        final_text = self.debug_logger.handle_turn_completed(event, payload, final_text, tool_executions_list)
                    
                    # Handle turn_failed events
                    elif event_type in ["turn_failed", "TurnFailed"] or type(event).__name__ == "TurnFailed":
                        stream_error = self.debug_logger.handle_turn_failed(event, payload, stream_error, tool_calls_made, printed_tool_calls)
                            
            except Exception as e:  
                stream_error = e
                error_msg = str(e)
                error_type = type(e).__name__
                
                # Check if it's a timeout or connection error
                if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    logger.warning(f"Stream timed out after {chunk_count} chunks - some tool responses may be missing")
                elif "connection" in error_msg.lower() or "closed" in error_msg.lower():
                    logger.warning(f"Stream connection closed after {chunk_count} chunks - some tool responses may be missing")
                elif "No response available" in error_msg:
                    logger.warning(f"Stream ended with 'No response available' after {chunk_count} chunks")
                else:
                    logger.error(f"Stream processing error after {chunk_count} chunks: {e}")
                
                if self.debug:
                    logger.exception("Full error:")
            
            # Calculate stream duration
            stream_duration = time.time() - stream_start_time
            
            # Log stream completion status with diagnostics
            if not turn_completed:
                logger.warning(f"Stream ended without turn_completed event")
                logger.warning(f"  - Processed {chunk_count} chunks over {stream_duration:.2f}s")
                logger.warning(f"  - Tool calls made: {len(tool_calls_made)}")
                logger.warning(f"  - Tool calls printed: {len(printed_tool_calls)}")
                if last_event_type:
                    logger.warning(f"  - Last event type: {last_event_type} (class: {last_event_class})")
                if stream_error:
                    logger.warning(f"  - Error: {type(stream_error).__name__}: {str(stream_error)}")
            elif self.debug:
                logger.debug(f"Stream completed successfully with turn_completed event ({chunk_count} chunks processed in {stream_duration:.2f}s)")
            
            # Fallback: Print any tool calls we tracked but never printed (stream ended early)
            if tool_calls_made:
                incomplete_calls = [tc for tc in tool_calls_made if getattr(tc, 'call_id', None) not in printed_tool_calls]
                if incomplete_calls:
                    self.debug_logger.print_stream_termination_diagnostics(
                        chunk_count, stream_duration, turn_completed,
                        last_event_type, last_event_class, stream_error,
                        incomplete_calls, tool_calls_made, printed_tool_calls
                    )
            
            # Check if we have incomplete tool calls
            incomplete_calls = len(tool_calls_made) - len(printed_tool_calls)
            if incomplete_calls > 0:
                logger.warning(f"  Stream ended early - {incomplete_calls} tool call(s) did not receive step_complete events")
                logger.warning("   This may indicate a timeout or the agent not waiting for tool responses")
            
            return {  
                "status": "success" if tool_execution_count > 0 else "completed",  
                "tool_executions": tool_execution_count,
                "tool_executions_list": tool_executions_list,
                "final_text": final_text,
                "incomplete_calls": incomplete_calls
            }
          
        except Exception as e:  
            logger.exception(f"✗ ERROR during agent execution: {e}")  
            return {  
                "status": "error",  
                "error": str(e)  
            }  
        finally:  
            logger.info("=" * 60)  
            logger.info("STEP 2 COMPLETE: Agent Invocation Finished")  
            logger.info("=" * 60)
      
def extract_from_slack_message(message_blocks: list) -> Dict[str, Any]:
    """
    Extract remediation information from Slack message blocks
    Supports multiple issue types: quota_exceeded, pod_crashloop, oom, image_pull_backoff, etc.
    """
    data = {
        "issue_type": "unknown",
        "namespace": "unknown",
        "resource_kind": "unknown",
        "resource_name": "unknown",
        "event_reason": "unknown",
        "issue_details": {}
    }
    
    for block in message_blocks:
        if block.get("type") == "section" and "text" in block:
            text = block["text"].get("text", "")
            
            # Extract Issue Type: "Kubernetes Issue Detected: `Quota Exceeded`"
            issue_match = re.search(r'Kubernetes Issue Detected:\*\*?\s+`([^`]+)`', text)
            if issue_match:
                issue_type_raw = issue_match.group(1).lower()
                # Map to our issue types
                if "quota" in issue_type_raw or "quota exceeded" in issue_type_raw:
                    data["issue_type"] = "quota_exceeded"
                elif "crashloop" in issue_type_raw or "crash loop" in issue_type_raw:
                    data["issue_type"] = "pod_crashloop"
                elif "oom" in issue_type_raw or "out of memory" in issue_type_raw:
                    data["issue_type"] = "oom"
                elif "image" in issue_type_raw and "pull" in issue_type_raw:
                    data["issue_type"] = "image_pull_backoff"
                else:
                    data["issue_type"] = "pod_failure"
                logger.info(f"Extracted issue type: {data['issue_type']}")
            
            # Extract Resource info: "Resource: ReplicaSet `name` in `namespace`"
            resource_match = re.search(r'Resource:\*\*?\s+(\w+)\s+`([^`]+)`\s+in\s+`([^`]+)`', text)
            if resource_match:
                data["resource_kind"] = resource_match.group(1)
                data["resource_name"] = resource_match.group(2)
                data["namespace"] = resource_match.group(3)
                logger.info(f"Extracted resource: {data['resource_kind']}/{data['resource_name']} in {data['namespace']}")
            
            # Extract Event Reason: "Event Reason:** `FailedCreate`"
            reason_match = re.search(r'Event Reason:\*\*?\s+`([^`]+)`', text)
            if reason_match:
                data["event_reason"] = reason_match.group(1)
                logger.info(f"Extracted reason: {data['event_reason']}")
            
            # Extract Quota Details (for quota_exceeded issues)
            if "Issue Details:" in text or "Quota Details:" in text:
                # Quota Name: `test-quota`
                name_match = re.search(r'Quota Name:\s+`([^`]+)`', text)
                if name_match:
                    data["issue_details"]["quota_name"] = name_match.group(1)
                
                # Resource Type: `requests.cpu`
                type_match = re.search(r'Resource Type:\s+`([^`]+)`', text)
                if type_match:
                    data["issue_details"]["resource_type"] = type_match.group(1)
                
                # Requested: `2`
                req_match = re.search(r'Resource Value:\s+`([^`]+)`|Requested:\s+`([^`]+)`', text)
                if req_match:
                    data["issue_details"]["requested"] = req_match.group(1) or req_match.group(2)
                
                # Limit: `1`
                limit_match = re.search(r'Limit:\s+`([^`]+)`|Current Limit:\s+`([^`]+)`', text)
                if limit_match:
                    data["issue_details"]["current_limit"] = limit_match.group(1) or limit_match.group(2)
                
                logger.info(f"Extracted quota details: {data['issue_details']}")
            
            # Extract container name for pod issues
            container_match = re.search(r'Container:\s+`([^`]+)`|container[:\s]+`([^`]+)`', text, re.IGNORECASE)
            if container_match:
                data["issue_details"]["container_name"] = container_match.group(1) or container_match.group(2)
    
    return data

def run_remediation_async(remediation_data: RemediationRequest, response_url: str):
    """Execute remediation in background and post results to Slack"""
    # Send immediate confirmation
    if response_url:
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "text": f"Remediation triggered for namespace *{remediation_data.namespace}*"
            }, headers={"Content-Type": "application/json"})
            logger.info("Posted immediate confirmation to Slack")
        except Exception as e:
            logger.error(f"Failed to post immediate confirmation: {e}")
    
    try:
        logger.info(f"Starting async remediation for namespace: {remediation_data.namespace}")
        result = remediation_agent.execute_remediation(remediation_data)
        
        # Build result message
        status = result.get("status", "error")
        if status == "success" or status == "completed":
            tool_executions = result.get("tool_executions", 0)
            final_text = result.get("final_text", "")
            if final_text:
                # Truncate very long messages for Slack
                if len(final_text) > 1000:
                    final_text = final_text[:1000] + "... (truncated)"
                message = f"✅ Remediation completed successfully ({tool_executions} tool executions)\n\n{final_text}"
            else:
                message = f"✅ Remediation completed successfully ({tool_executions} tool executions)"
        else:
            error_msg = result.get("error", result.get("final_text", "Unknown error"))
            message = f"❌ Remediation failed: {error_msg}"
    except Exception as e:
        logger.error(f"Remediation error: {e}")
        message = f"❌ Remediation error: {str(e)}"
    
    # Post result to Slack
    if response_url:
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "text": message
            }, headers={"Content-Type": "application/json"})
            logger.info(f"Posted result to Slack: {message[:100]}...")
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")

def initialize_agent():
    """Initialize the remediation agent on startup"""
    global remediation_agent
    debug_mode = os.getenv("DEBUG", "false").lower() == "true"
    remediation_agent = OCPMCPRemediationAgent(debug=debug_mode)
    remediation_agent.register_ocp_mcp_server()
    logger.info("Remediation agent initialized")

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "OCP MCP Remediation Agent API is running",
        "endpoints": ["/remediate", "/health"],
        "supported_issue_types": ["pod_crashloop", "oom", "pod_failure", "image_pull_backoff", "quota_exceeded"]
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "agent_initialized": remediation_agent is not None,
        "client_initialized": remediation_agent.client is not None if remediation_agent else False,
        "model": remediation_agent.model_id if remediation_agent else "not initialized"
    }

@app.post("/remediate", response_model=RemediationResponse)
async def remediate(http_request: Request, payload: Optional[str] = Form(None)):
    """
    Execute automated remediation for the given Kubernetes issue.
    Handles both JSON and Slack form-encoded payloads.
    
    Supports multiple issue types:
    - pod_crashloop: Pod crash loop issues
    - oom: Out of memory issues
    - pod_failure: General pod failures
    - image_pull_backoff: Image pull failures
    - quota_exceeded: Resource quota exceeded
    
    Example requests:
    
    1. Pod Crash Loop (OOM):
    ```bash
    curl -X POST http://localhost:8080/remediate \\
      -H "Content-Type: application/json" \\
      -d '{
        "issue_type": "pod_crashloop",
        "namespace": "failure-test",
        "resource": {
          "kind": "Pod",
          "name": "oom-app_failure-test",
          "uid": "21c593e6-fde6-4767-9501-2c89fb3a053e"
        },
        "event_reason": "Back-off restarting failed container",
        "issue_details": {
          "container_name": "memory-hog"
        },
        "remediation_strategy": "auto"
      }'
    ```
    
    2. Quota Exceeded:
    ```bash
    curl -X POST http://localhost:8080/remediate \\
      -H "Content-Type: application/json" \\
      -d '{
        "issue_type": "quota_exceeded",
        "namespace": "my-namespace",
        "resource": {
          "kind": "Pod",
          "name": "my-pod"
        },
        "event_reason": "FailedCreate",
        "issue_details": {
          "quota_name": "test-quota",
          "resource_type": "requests.cpu",
          "requested": "2",
          "current_limit": "1"
        },
        "remediation_strategy": "auto"
      }'
    ```
    """
    logger.info("=" * 60)
    logger.info("RAW REMEDIATION REQUEST RECEIVED")
    logger.info(f"Method: {http_request.method}")
    logger.info(f"URL: {http_request.url}")
    logger.info(f"Content-Type: {http_request.headers.get('content-type')}")
    logger.info(f"Has form payload: {payload is not None}")
    if payload:
        logger.info(f"Payload (first 500 chars): {payload[:500]}")
    logger.info("=" * 60)
    
    remediation_data = None
    response_url = None
    
    # Handle Slack form-encoded payload
    if payload:
        logger.info("Detected Slack form payload")
        try:
            slack_data = json.loads(payload)
            logger.info(f"Slack payload type: {slack_data.get('type')}")
            
            # Extract response_url for async callback
            response_url = slack_data.get("response_url")
            logger.info(f"Slack response_url: {response_url}")
            
            if "message" in slack_data and "blocks" in slack_data["message"]:
                extracted = extract_from_slack_message(slack_data["message"]["blocks"])
                
                remediation_data = RemediationRequest(
                    issue_type=extracted["issue_type"],
                    namespace=extracted["namespace"],
                    resource={
                        "kind": extracted["resource_kind"],
                        "name": extracted["resource_name"]
                    },
                    event_reason=extracted["event_reason"],
                    issue_details=extracted["issue_details"],
                    remediation_strategy="auto"
                )
                logger.info("Successfully parsed Slack payload")
            else:
                raise HTTPException(status_code=400, detail="No message blocks found in Slack payload")
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Slack JSON: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid JSON in payload: {e}")
    else:
        # Handle JSON body
        logger.info("Attempting to parse as JSON body")
        try:
            json_body = await http_request.json()
            remediation_data = RemediationRequest(**json_body)
            logger.info("Successfully parsed JSON body")
        except Exception as e:
            logger.error(f"Failed to parse as JSON: {e}")
            raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    logger.info("PARSED REMEDIATION REQUEST:")
    logger.info(f"Issue Type: {remediation_data.issue_type}")
    logger.info(f"Namespace: {remediation_data.namespace}")
    logger.info(f"Resource: {remediation_data.resource}")
    logger.info(f"Event Reason: {remediation_data.event_reason}")
    logger.info(f"Issue Details: {remediation_data.issue_details}")
    logger.info("=" * 60)
    
    # If Slack request with response_url, run async and return immediately
    if response_url:
        logger.info("Launching async remediation for Slack")
        thread = threading.Thread(
            target=run_remediation_async,
            args=(remediation_data, response_url),
            daemon=True
        )
        thread.start()
        return {"text": "Remediation in progress...", "response_type": "ephemeral"}
    
    # Otherwise, run synchronously (for direct API calls)
    if not remediation_agent:
        raise HTTPException(status_code=503, detail="Remediation agent not initialized")
    
    if not remediation_agent.client or not remediation_agent.model_id:
        raise HTTPException(status_code=503, detail="Client not initialized")
    
    if not remediation_data.namespace or not remediation_data.namespace.strip():
        raise HTTPException(status_code=400, detail="Namespace cannot be empty")
    
    if not remediation_data.resource.get("kind") or not remediation_data.resource.get("name"):
        raise HTTPException(status_code=400, detail="Resource must have 'kind' and 'name'")
    
    try:
        result = remediation_agent.execute_remediation(remediation_data)
        
        logger.info(f"Remediation result: {result.get('status')}")
        return RemediationResponse(
            status=result.get("status", "error"),
            message=result.get("final_text", result.get("error", "Unknown result")),
            details={
                "tool_executions": result.get("tool_executions", 0),
                "tool_executions_list": result.get("tool_executions_list", []),
                "incomplete_calls": result.get("incomplete_calls", 0),
                "namespace": remediation_data.namespace,
                "resource": remediation_data.resource,
                "issue_type": remediation_data.issue_type
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in remediate endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing remediation: {str(e)}")

if __name__ == "__main__":
    """
    Run the FastAPI server.
    
    Example:
    uvicorn remediation-agent-lls3:app --host 0.0.0.0 --port 8080 --reload
    """
    uvicorn.run(app, host="0.0.0.0", port=8080)