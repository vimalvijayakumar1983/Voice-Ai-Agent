'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import {
  X,
  Send,
  RotateCcw,
  ChevronRight,
  ChevronLeft,
  Eye,
  Bot,
  User,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { cn } from '@/lib/utils';
import { useWorkflowStore } from '@/stores/workflow-store';

// ============================================================================
// Types
// ============================================================================

interface SimulationStep {
  stepIndex: number;
  nodeId: string;
  nodeType: string;
  nodeLabel: string;
  agentMessage?: string;
  contactInput?: string;
  variablesSnapshot: Record<string, unknown>;
  status: string;
  output: Record<string, unknown>;
}

interface ConversationMessage {
  role: 'agent' | 'contact' | 'system';
  content: string;
  nodeId?: string;
  timestamp: string;
}

// ============================================================================
// Component
// ============================================================================

interface WorkflowSimulatorPanelProps {
  onClose: () => void;
  workflowId?: string;
}

export function WorkflowSimulatorPanel({
  onClose,
  workflowId,
}: WorkflowSimulatorPanelProps) {
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [steps, setSteps] = useState<SimulationStep[]>([]);
  const [currentStepIndex, setCurrentStepIndex] = useState(0);
  const [input, setInput] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [executionId, setExecutionId] = useState<string | null>(null);
  const [variables, setVariables] = useState<Record<string, unknown>>({});
  const [activeTab, setActiveTab] = useState<'chat' | 'trace' | 'variables'>('chat');

  const chatEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const startSimulation = useCallback(async () => {
    setMessages([]);
    setSteps([]);
    setCurrentStepIndex(0);
    setVariables({});
    setIsProcessing(true);

    try {
      // In a real implementation, this would call the API
      // For now, simulate the start
      const mockExecId = `sim_${Date.now()}`;
      setExecutionId(mockExecId);

      setMessages([
        {
          role: 'system',
          content: 'Simulation started. The workflow will execute from the first node.',
          timestamp: new Date().toISOString(),
        },
      ]);

      // Simulate initial greeting
      setTimeout(() => {
        const greetingMsg: ConversationMessage = {
          role: 'agent',
          content: 'Hello! Good afternoon. How can I help you today?',
          nodeId: 'greeting_1',
          timestamp: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, greetingMsg]);
        setSteps([
          {
            stepIndex: 0,
            nodeId: 'greeting_1',
            nodeType: 'GREETING',
            nodeLabel: 'Greeting',
            agentMessage: greetingMsg.content,
            variablesSnapshot: { _timeOfDay: 'afternoon' },
            status: 'executed',
            output: { timeOfDay: 'afternoon' },
          },
        ]);
        setVariables({ _timeOfDay: 'afternoon' });
        setIsProcessing(false);
      }, 500);
    } catch (error) {
      setIsProcessing(false);
      setMessages((prev) => [
        ...prev,
        {
          role: 'system',
          content: `Error starting simulation: ${error}`,
          timestamp: new Date().toISOString(),
        },
      ]);
    }
  }, [workflowId]);

  const handleSendMessage = useCallback(async () => {
    if (!input.trim() || isProcessing) return;

    const userMessage: ConversationMessage = {
      role: 'contact',
      content: input.trim(),
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMessage]);
    setInput('');
    setIsProcessing(true);

    // Simulate processing
    setTimeout(() => {
      const responseMsg: ConversationMessage = {
        role: 'agent',
        content: 'Thank you for your response. Let me process that.',
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, responseMsg]);

      const newStep: SimulationStep = {
        stepIndex: steps.length,
        nodeId: `node_${steps.length}`,
        nodeType: 'INFO_CAPTURE',
        nodeLabel: 'Process Input',
        agentMessage: responseMsg.content,
        contactInput: userMessage.content,
        variablesSnapshot: { ...variables, _lastInput: userMessage.content },
        status: 'executed',
        output: { processed: true },
      };
      setSteps((prev) => [...prev, newStep]);
      setCurrentStepIndex(steps.length);
      setVariables((prev) => ({ ...prev, _lastInput: userMessage.content }));
      setIsProcessing(false);
    }, 800);
  }, [input, isProcessing, steps, variables]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSendMessage();
      }
    },
    [handleSendMessage],
  );

  const stepForward = useCallback(() => {
    if (currentStepIndex < steps.length - 1) {
      setCurrentStepIndex((prev) => prev + 1);
    }
  }, [currentStepIndex, steps.length]);

  const stepBackward = useCallback(() => {
    if (currentStepIndex > 0) {
      setCurrentStepIndex((prev) => prev - 1);
    }
  }, [currentStepIndex]);

  return (
    <div className="w-[400px] border-l bg-card flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">Workflow Simulator</h3>
          {executionId && (
            <Badge variant="outline" className="text-[10px]">
              {isProcessing ? 'Processing...' : 'Ready'}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={startSimulation} title="Reset">
            <RotateCcw className="w-3.5 h-3.5" />
          </Button>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={onClose}>
            <X className="w-3.5 h-3.5" />
          </Button>
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex border-b">
        {(['chat', 'trace', 'variables'] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              'flex-1 px-3 py-2 text-xs font-medium transition-colors',
              activeTab === tab
                ? 'text-primary border-b-2 border-primary'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {tab === 'chat' ? 'Conversation' : tab === 'trace' ? 'Execution Trace' : 'Variables'}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden flex flex-col">
        {/* Chat Tab */}
        {activeTab === 'chat' && (
          <>
            <div className="flex-1 overflow-y-auto p-3 space-y-3">
              {messages.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full text-center">
                  <Bot className="w-10 h-10 text-muted-foreground mb-3" />
                  <p className="text-sm font-medium">Ready to simulate</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    Click the reset button to start a new simulation
                  </p>
                  <Button size="sm" className="mt-4" onClick={startSimulation}>
                    Start Simulation
                  </Button>
                </div>
              ) : (
                messages.map((msg, i) => (
                  <div
                    key={i}
                    className={cn(
                      'flex gap-2',
                      msg.role === 'contact' && 'flex-row-reverse',
                      msg.role === 'system' && 'justify-center',
                    )}
                  >
                    {msg.role !== 'system' && (
                      <div
                        className={cn(
                          'w-7 h-7 rounded-full flex items-center justify-center shrink-0',
                          msg.role === 'agent' ? 'bg-primary/10' : 'bg-gray-100 dark:bg-gray-800',
                        )}
                      >
                        {msg.role === 'agent' ? (
                          <Bot className="w-3.5 h-3.5 text-primary" />
                        ) : (
                          <User className="w-3.5 h-3.5 text-muted-foreground" />
                        )}
                      </div>
                    )}
                    <div
                      className={cn(
                        'rounded-lg px-3 py-2 max-w-[80%] text-sm',
                        msg.role === 'agent' && 'bg-primary/10 text-foreground',
                        msg.role === 'contact' && 'bg-gray-100 dark:bg-gray-800 text-foreground',
                        msg.role === 'system' &&
                          'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-800 dark:text-yellow-200 text-xs px-3 py-1',
                      )}
                    >
                      {msg.content}
                    </div>
                  </div>
                ))
              )}
              <div ref={chatEndRef} />
            </div>

            {/* Input */}
            {executionId && (
              <div className="border-t p-3">
                <div className="flex gap-2">
                  <Input
                    ref={inputRef}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Type a response..."
                    disabled={isProcessing}
                    className="h-8 text-sm"
                  />
                  <Button
                    size="sm"
                    className="h-8 w-8 p-0"
                    onClick={handleSendMessage}
                    disabled={!input.trim() || isProcessing}
                  >
                    {isProcessing ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Send className="w-3.5 h-3.5" />
                    )}
                  </Button>
                </div>
              </div>
            )}
          </>
        )}

        {/* Trace Tab */}
        {activeTab === 'trace' && (
          <div className="flex-1 overflow-y-auto">
            {/* Step navigation */}
            {steps.length > 0 && (
              <div className="flex items-center justify-between px-3 py-2 border-b">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 w-6 p-0"
                  onClick={stepBackward}
                  disabled={currentStepIndex === 0}
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </Button>
                <span className="text-xs text-muted-foreground">
                  Step {currentStepIndex + 1} of {steps.length}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 w-6 p-0"
                  onClick={stepForward}
                  disabled={currentStepIndex >= steps.length - 1}
                >
                  <ChevronRight className="w-3.5 h-3.5" />
                </Button>
              </div>
            )}

            <div className="p-3 space-y-2">
              {steps.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-8">
                  No execution steps yet
                </p>
              ) : (
                steps.map((step, i) => (
                  <Card
                    key={i}
                    className={cn(
                      'cursor-pointer transition-colors',
                      i === currentStepIndex && 'ring-2 ring-primary',
                    )}
                    onClick={() => setCurrentStepIndex(i)}
                  >
                    <CardContent className="p-2.5">
                      <div className="flex items-center gap-2">
                        <div
                          className={cn(
                            'w-5 h-5 rounded-full flex items-center justify-center shrink-0',
                            step.status === 'executed'
                              ? 'bg-green-100 dark:bg-green-900/30'
                              : step.status === 'failed'
                                ? 'bg-red-100 dark:bg-red-900/30'
                                : step.status === 'waiting'
                                  ? 'bg-yellow-100 dark:bg-yellow-900/30'
                                  : 'bg-gray-100',
                          )}
                        >
                          {step.status === 'executed' && (
                            <CheckCircle2 className="w-3 h-3 text-green-600" />
                          )}
                          {step.status === 'failed' && (
                            <XCircle className="w-3 h-3 text-red-600" />
                          )}
                          {step.status === 'waiting' && (
                            <Clock className="w-3 h-3 text-yellow-600" />
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium truncate">
                            {step.nodeLabel}
                          </p>
                          <p className="text-[10px] text-muted-foreground">
                            {step.nodeType}
                          </p>
                        </div>
                        <span className="text-[10px] text-muted-foreground">
                          #{step.stepIndex + 1}
                        </span>
                      </div>
                      {step.agentMessage && (
                        <p className="text-[11px] text-muted-foreground mt-1.5 line-clamp-2">
                          {step.agentMessage}
                        </p>
                      )}
                    </CardContent>
                  </Card>
                ))
              )}
            </div>
          </div>
        )}

        {/* Variables Tab */}
        {activeTab === 'variables' && (
          <div className="flex-1 overflow-y-auto p-3">
            {Object.keys(variables).length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-8">
                No variables set yet
              </p>
            ) : (
              <div className="space-y-1">
                {Object.entries(variables).map(([key, value]) => (
                  <div
                    key={key}
                    className="flex items-start justify-between py-1.5 border-b border-dashed"
                  >
                    <span className="text-xs font-mono text-muted-foreground">
                      {key}
                    </span>
                    <span className="text-xs font-mono text-foreground max-w-[50%] text-right truncate">
                      {typeof value === 'object'
                        ? JSON.stringify(value)
                        : String(value)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
