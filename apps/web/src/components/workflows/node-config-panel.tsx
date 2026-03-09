'use client';

import { useCallback, useMemo } from 'react';
import { X, AlertCircle, Plus, Trash2 } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { useWorkflowStore, WorkflowNodeType, type WorkflowNodeData } from '@/stores/workflow-store';

// ============================================================================
// Component
// ============================================================================

export function NodeConfigPanel() {
  const { nodes, selectedNodeIds, updateNode, removeNodes } = useWorkflowStore();

  const selectedNode = useMemo(() => {
    if (selectedNodeIds.length !== 1) return null;
    return nodes.find((n) => n.id === selectedNodeIds[0]) || null;
  }, [nodes, selectedNodeIds]);

  const handleClose = useCallback(() => {
    useWorkflowStore.getState().clearSelection();
  }, []);

  const handleUpdateField = useCallback(
    (field: string, value: unknown) => {
      if (!selectedNode) return;
      updateNode(selectedNode.id, { [field]: value } as Partial<WorkflowNodeData>);
    },
    [selectedNode, updateNode],
  );

  const handleUpdateConfig = useCallback(
    (key: string, value: unknown) => {
      if (!selectedNode) return;
      const config = { ...(selectedNode.data.config || {}), [key]: value };
      updateNode(selectedNode.id, { config });
    },
    [selectedNode, updateNode],
  );

  const handleDelete = useCallback(() => {
    if (!selectedNode) return;
    removeNodes([selectedNode.id]);
  }, [selectedNode, removeNodes]);

  if (!selectedNode) {
    return (
      <div className="w-80 border-l bg-card flex items-center justify-center p-6">
        <p className="text-sm text-muted-foreground text-center">
          Select a node to configure it
        </p>
      </div>
    );
  }

  const config = (selectedNode.data.config || {}) as Record<string, unknown>;

  return (
    <div className="w-80 border-l bg-card flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b">
        <h3 className="text-sm font-semibold">Configure Node</h3>
        <button onClick={handleClose} className="text-muted-foreground hover:text-foreground">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Basic Fields */}
        <div className="space-y-3">
          <div>
            <Label className="text-xs">Label</Label>
            <Input
              value={selectedNode.data.label}
              onChange={(e) => handleUpdateField('label', e.target.value)}
              className="h-8 text-sm mt-1"
            />
          </div>

          <div>
            <Label className="text-xs">Node Type</Label>
            <Input
              value={selectedNode.data.type.replace(/_/g, ' ')}
              disabled
              className="h-8 text-sm mt-1"
            />
          </div>
        </div>

        <Separator />

        {/* Prompt Template */}
        <div>
          <Label className="text-xs">Prompt Template</Label>
          <Textarea
            value={selectedNode.data.prompt || ''}
            onChange={(e) => handleUpdateField('prompt', e.target.value)}
            placeholder="Enter the prompt text. Use {{variable}} for interpolation."
            className="mt-1 text-sm min-h-[80px]"
          />
          <p className="text-[10px] text-muted-foreground mt-1">
            Use {'{{variable_name}}'} to insert variables.
          </p>
        </div>

        <Separator />

        {/* Type-specific configuration */}
        <div className="space-y-3">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase">
            Type-Specific Config
          </h4>

          {selectedNode.data.type === WorkflowNodeType.VERIFICATION && (
            <VerificationConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.QUALIFICATION && (
            <QualificationConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.INFO_CAPTURE && (
            <InfoCaptureConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.OFFER_OPTIONS && (
            <OfferOptionsConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {(selectedNode.data.type === WorkflowNodeType.LOOKUP ||
            selectedNode.data.type === WorkflowNodeType.API_CALL) && (
            <ApiConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.SEND_MESSAGE && (
            <SendMessageConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.RETRY_LATER && (
            <RetryConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.CONDITIONAL && (
            <ConditionalConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.SET_VARIABLE && (
            <SetVariableConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.ESCALATE_HUMAN && (
            <EscalateConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.END_CALL && (
            <EndCallConfig config={config} onUpdate={handleUpdateConfig} />
          )}

          {selectedNode.data.type === WorkflowNodeType.COLLECT_PAYMENT && (
            <PaymentConfig config={config} onUpdate={handleUpdateConfig} />
          )}
        </div>

        <Separator />

        {/* Advanced */}
        <div className="space-y-3">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase">
            Advanced
          </h4>
          <div>
            <Label className="text-xs">Timeout (seconds)</Label>
            <Input
              type="number"
              value={selectedNode.data.timeoutSeconds || ''}
              onChange={(e) =>
                handleUpdateField('timeoutSeconds', parseInt(e.target.value) || undefined)
              }
              placeholder="30"
              className="h-8 text-sm mt-1"
            />
          </div>
          <div>
            <Label className="text-xs">Fallback Node ID</Label>
            <Input
              value={selectedNode.data.fallbackNodeId || ''}
              onChange={(e) => handleUpdateField('fallbackNodeId', e.target.value || undefined)}
              placeholder="node_id"
              className="h-8 text-sm mt-1"
            />
          </div>
        </div>

        <Separator />

        {/* Delete */}
        <Button variant="destructive" size="sm" className="w-full" onClick={handleDelete}>
          <Trash2 className="w-3.5 h-3.5 mr-1.5" />
          Delete Node
        </Button>
      </div>
    </div>
  );
}

// ============================================================================
// Type-Specific Configs
// ============================================================================

interface ConfigProps {
  config: Record<string, unknown>;
  onUpdate: (key: string, value: unknown) => void;
}

function VerificationConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Verification Field</Label>
        <Select
          value={(config.field as string) || 'date_of_birth'}
          onValueChange={(v) => onUpdate('field', v)}
        >
          <SelectTrigger className="h-8 text-sm mt-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="date_of_birth">Date of Birth</SelectItem>
            <SelectItem value="last_4_ssn">Last 4 SSN</SelectItem>
            <SelectItem value="account_number">Account Number</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label className="text-xs">Expected Value (optional)</Label>
        <Input
          value={(config.expectedValue as string) || ''}
          onChange={(e) => onUpdate('expectedValue', e.target.value)}
          placeholder="For exact match verification"
          className="h-8 text-sm mt-1"
        />
      </div>
    </div>
  );
}

function QualificationConfig({ config, onUpdate }: ConfigProps) {
  const questions = (config.questions as Array<{
    key: string;
    question: string;
    weight: number;
    passingAnswers?: string[];
  }>) || [];

  const addQuestion = () => {
    onUpdate('questions', [
      ...questions,
      { key: `q${questions.length + 1}`, question: '', weight: 1, passingAnswers: [] },
    ]);
  };

  const updateQuestion = (index: number, field: string, value: unknown) => {
    const updated = [...questions];
    (updated[index] as Record<string, unknown>)[field] = value;
    onUpdate('questions', updated);
  };

  const removeQuestion = (index: number) => {
    onUpdate('questions', questions.filter((_, i) => i !== index));
  };

  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Qualification Threshold (%)</Label>
        <Input
          type="number"
          value={(config.threshold as number) || 60}
          onChange={(e) => onUpdate('threshold', parseInt(e.target.value) || 60)}
          className="h-8 text-sm mt-1"
        />
      </div>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label className="text-xs">Questions</Label>
          <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={addQuestion}>
            <Plus className="w-3 h-3 mr-1" /> Add
          </Button>
        </div>
        {questions.map((q, i) => (
          <Card key={i} className="p-2">
            <div className="space-y-1.5">
              <Input
                value={q.question}
                onChange={(e) => updateQuestion(i, 'question', e.target.value)}
                placeholder="Question text"
                className="h-7 text-xs"
              />
              <div className="flex gap-1.5">
                <Input
                  value={q.weight}
                  type="number"
                  onChange={(e) => updateQuestion(i, 'weight', parseInt(e.target.value) || 1)}
                  placeholder="Weight"
                  className="h-7 text-xs w-20"
                />
                <Input
                  value={(q.passingAnswers || []).join(', ')}
                  onChange={(e) =>
                    updateQuestion(
                      i,
                      'passingAnswers',
                      e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                    )
                  }
                  placeholder="Passing answers (comma-separated)"
                  className="h-7 text-xs flex-1"
                />
                <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={() => removeQuestion(i)}>
                  <Trash2 className="w-3 h-3" />
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function InfoCaptureConfig({ config, onUpdate }: ConfigProps) {
  const fields = (config.fields as Array<{
    key: string;
    label: string;
    required?: boolean;
    validation?: string;
  }>) || [];

  const addField = () => {
    onUpdate('fields', [
      ...fields,
      { key: `field${fields.length + 1}`, label: '', required: true },
    ]);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs">Fields to Capture</Label>
        <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={addField}>
          <Plus className="w-3 h-3 mr-1" /> Add
        </Button>
      </div>
      {fields.map((f, i) => (
        <div key={i} className="flex gap-1.5">
          <Input
            value={f.key}
            onChange={(e) => {
              const updated = [...fields];
              updated[i] = { ...updated[i], key: e.target.value };
              onUpdate('fields', updated);
            }}
            placeholder="Key"
            className="h-7 text-xs w-24"
          />
          <Input
            value={f.label}
            onChange={(e) => {
              const updated = [...fields];
              updated[i] = { ...updated[i], label: e.target.value };
              onUpdate('fields', updated);
            }}
            placeholder="Label"
            className="h-7 text-xs flex-1"
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            onClick={() => onUpdate('fields', fields.filter((_, j) => j !== i))}
          >
            <Trash2 className="w-3 h-3" />
          </Button>
        </div>
      ))}
    </div>
  );
}

function OfferOptionsConfig({ config, onUpdate }: ConfigProps) {
  const options = (config.options as Array<{
    key: string;
    label: string;
    description?: string;
  }>) || [];

  const addOption = () => {
    onUpdate('options', [
      ...options,
      { key: `opt${options.length + 1}`, label: '', description: '' },
    ]);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs">Menu Options</Label>
        <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={addOption}>
          <Plus className="w-3 h-3 mr-1" /> Add
        </Button>
      </div>
      {options.map((o, i) => (
        <div key={i} className="flex gap-1.5">
          <Input
            value={o.label}
            onChange={(e) => {
              const updated = [...options];
              updated[i] = { ...updated[i], label: e.target.value, key: e.target.value.toLowerCase().replace(/\s+/g, '_') };
              onUpdate('options', updated);
            }}
            placeholder="Option label"
            className="h-7 text-xs flex-1"
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            onClick={() => onUpdate('options', options.filter((_, j) => j !== i))}
          >
            <Trash2 className="w-3 h-3" />
          </Button>
        </div>
      ))}
    </div>
  );
}

function ApiConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">URL</Label>
        <Input
          value={(config.url as string) || (config.endpoint as string) || ''}
          onChange={(e) => {
            onUpdate('url', e.target.value);
            onUpdate('endpoint', e.target.value);
          }}
          placeholder="https://api.example.com/endpoint"
          className="h-8 text-sm mt-1"
        />
      </div>
      <div>
        <Label className="text-xs">Method</Label>
        <Select
          value={(config.method as string) || 'GET'}
          onValueChange={(v) => onUpdate('method', v)}
        >
          <SelectTrigger className="h-8 text-sm mt-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="GET">GET</SelectItem>
            <SelectItem value="POST">POST</SelectItem>
            <SelectItem value="PUT">PUT</SelectItem>
            <SelectItem value="PATCH">PATCH</SelectItem>
            <SelectItem value="DELETE">DELETE</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label className="text-xs">Headers (JSON)</Label>
        <Textarea
          value={JSON.stringify(config.headers || {}, null, 2)}
          onChange={(e) => {
            try {
              onUpdate('headers', JSON.parse(e.target.value));
            } catch {
              // ignore invalid JSON while typing
            }
          }}
          placeholder='{"Authorization": "Bearer {{token}}"}'
          className="text-xs mt-1 min-h-[60px] font-mono"
        />
      </div>
      <div>
        <Label className="text-xs">Result Variable Name</Label>
        <Input
          value={(config.resultVariable as string) || '_apiResult'}
          onChange={(e) => onUpdate('resultVariable', e.target.value)}
          className="h-8 text-sm mt-1"
        />
      </div>
    </div>
  );
}

function SendMessageConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Channel</Label>
        <Select
          value={(config.channel as string) || 'sms'}
          onValueChange={(v) => onUpdate('channel', v)}
        >
          <SelectTrigger className="h-8 text-sm mt-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="sms">SMS</SelectItem>
            <SelectItem value="email">Email</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label className="text-xs">Recipient</Label>
        <Input
          value={(config.recipient as string) || ''}
          onChange={(e) => onUpdate('recipient', e.target.value)}
          placeholder="{{contact_phone}} or specific number"
          className="h-8 text-sm mt-1"
        />
      </div>
      <div>
        <Label className="text-xs">Message Body</Label>
        <Textarea
          value={(config.messageBody as string) || ''}
          onChange={(e) => onUpdate('messageBody', e.target.value)}
          placeholder="Message text with {{variables}}"
          className="text-sm mt-1 min-h-[60px]"
        />
      </div>
    </div>
  );
}

function RetryConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Delay (minutes)</Label>
        <Input
          type="number"
          value={(config.delayMinutes as number) || 60}
          onChange={(e) => onUpdate('delayMinutes', parseInt(e.target.value) || 60)}
          className="h-8 text-sm mt-1"
        />
      </div>
      <div>
        <Label className="text-xs">Max Retries</Label>
        <Input
          type="number"
          value={(config.maxRetries as number) || 3}
          onChange={(e) => onUpdate('maxRetries', parseInt(e.target.value) || 3)}
          className="h-8 text-sm mt-1"
        />
      </div>
    </div>
  );
}

function ConditionalConfig({ config, onUpdate }: ConfigProps) {
  const conditions = (config.conditions as Array<{ edgeId: string; expression: string }>) || [];

  const addCondition = () => {
    onUpdate('conditions', [...conditions, { edgeId: '', expression: '' }]);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs">Conditions</Label>
        <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={addCondition}>
          <Plus className="w-3 h-3 mr-1" /> Add
        </Button>
      </div>
      {conditions.map((c, i) => (
        <div key={i} className="space-y-1">
          <Input
            value={c.expression}
            onChange={(e) => {
              const updated = [...conditions];
              updated[i] = { ...updated[i], expression: e.target.value };
              onUpdate('conditions', updated);
            }}
            placeholder='e.g., _qualificationScore > 60'
            className="h-7 text-xs font-mono"
          />
        </div>
      ))}
      <p className="text-[10px] text-muted-foreground flex items-start gap-1">
        <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
        Expressions are evaluated top-to-bottom. First match wins.
      </p>
    </div>
  );
}

function SetVariableConfig({ config, onUpdate }: ConfigProps) {
  const assignments = (config.assignments as Array<{
    key: string;
    value: unknown;
    expression?: string;
  }>) || [];

  const addAssignment = () => {
    onUpdate('assignments', [...assignments, { key: '', value: '' }]);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs">Variable Assignments</Label>
        <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={addAssignment}>
          <Plus className="w-3 h-3 mr-1" /> Add
        </Button>
      </div>
      {assignments.map((a, i) => (
        <div key={i} className="flex gap-1.5">
          <Input
            value={a.key}
            onChange={(e) => {
              const updated = [...assignments];
              updated[i] = { ...updated[i], key: e.target.value };
              onUpdate('assignments', updated);
            }}
            placeholder="Variable name"
            className="h-7 text-xs w-28"
          />
          <Input
            value={String(a.value || a.expression || '')}
            onChange={(e) => {
              const updated = [...assignments];
              updated[i] = { ...updated[i], value: e.target.value };
              onUpdate('assignments', updated);
            }}
            placeholder="Value or expression"
            className="h-7 text-xs flex-1"
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            onClick={() => onUpdate('assignments', assignments.filter((_, j) => j !== i))}
          >
            <Trash2 className="w-3 h-3" />
          </Button>
        </div>
      ))}
    </div>
  );
}

function EscalateConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Department</Label>
        <Input
          value={(config.department as string) || ''}
          onChange={(e) => onUpdate('department', e.target.value)}
          placeholder="e.g., billing, support"
          className="h-8 text-sm mt-1"
        />
      </div>
      <div>
        <Label className="text-xs">Priority</Label>
        <Select
          value={(config.priority as string) || 'normal'}
          onValueChange={(v) => onUpdate('priority', v)}
        >
          <SelectTrigger className="h-8 text-sm mt-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="low">Low</SelectItem>
            <SelectItem value="normal">Normal</SelectItem>
            <SelectItem value="high">High</SelectItem>
            <SelectItem value="urgent">Urgent</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label className="text-xs">Reason</Label>
        <Input
          value={(config.reason as string) || ''}
          onChange={(e) => onUpdate('reason', e.target.value)}
          placeholder="Reason for escalation"
          className="h-8 text-sm mt-1"
        />
      </div>
    </div>
  );
}

function EndCallConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Outcome</Label>
        <Select
          value={(config.outcome as string) || 'completed'}
          onValueChange={(v) => onUpdate('outcome', v)}
        >
          <SelectTrigger className="h-8 text-sm mt-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="completed">Completed</SelectItem>
            <SelectItem value="converted">Converted</SelectItem>
            <SelectItem value="not_interested">Not Interested</SelectItem>
            <SelectItem value="callback_scheduled">Callback Scheduled</SelectItem>
            <SelectItem value="do_not_call">Do Not Call</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label className="text-xs">Disposition</Label>
        <Input
          value={(config.disposition as string) || ''}
          onChange={(e) => onUpdate('disposition', e.target.value)}
          placeholder="e.g., normal, complaint"
          className="h-8 text-sm mt-1"
        />
      </div>
    </div>
  );
}

function PaymentConfig({ config, onUpdate }: ConfigProps) {
  return (
    <div className="space-y-2">
      <div>
        <Label className="text-xs">Amount</Label>
        <Input
          type="number"
          value={(config.amount as number) || ''}
          onChange={(e) => onUpdate('amount', parseFloat(e.target.value) || undefined)}
          placeholder="0.00"
          className="h-8 text-sm mt-1"
        />
      </div>
      <div>
        <Label className="text-xs">Currency</Label>
        <Select
          value={(config.currency as string) || 'USD'}
          onValueChange={(v) => onUpdate('currency', v)}
        >
          <SelectTrigger className="h-8 text-sm mt-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="USD">USD</SelectItem>
            <SelectItem value="EUR">EUR</SelectItem>
            <SelectItem value="GBP">GBP</SelectItem>
            <SelectItem value="CAD">CAD</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
