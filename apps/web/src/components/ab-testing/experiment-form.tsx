'use client';

import { useState, useCallback } from 'react';
import { Plus, Trash2, GripVertical } from 'lucide-react';
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
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { useCreateExperiment, type CreateExperimentData } from '@/hooks/use-ab-testing';
import { useApiQuery } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

interface VariantFormData {
  name: string;
  promptText: string;
  trafficPercentage: number;
  isControl: boolean;
}

interface ExperimentFormProps {
  onSuccess?: (id: string) => void;
  onCancel?: () => void;
}

// ============================================================================
// Component
// ============================================================================

export function ExperimentForm({ onSuccess, onCancel }: ExperimentFormProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [agentId, setAgentId] = useState('');
  const [splitMethod, setSplitMethod] = useState('PERCENTAGE');
  const [successCriteria, setSuccessCriteria] = useState('success_rate');
  const [minSamples, setMinSamples] = useState(100);
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.95);
  const [variants, setVariants] = useState<VariantFormData[]>([
    { name: 'Control', promptText: '', trafficPercentage: 50, isControl: true },
    { name: 'Variant B', promptText: '', trafficPercentage: 50, isControl: false },
  ]);

  const createMutation = useCreateExperiment();

  // Fetch agents for the selector
  const { data: agentsData } = useApiQuery<{ data: Array<{ id: string; name: string }> }>(
    ['agents'],
    '/agents?limit=100',
  );
  const agents = agentsData?.data || [];

  const addVariant = useCallback(() => {
    const newPercentage = Math.floor(100 / (variants.length + 1));
    const updatedVariants = variants.map((v) => ({
      ...v,
      trafficPercentage: newPercentage,
    }));
    updatedVariants.push({
      name: `Variant ${String.fromCharCode(65 + variants.length)}`,
      promptText: '',
      trafficPercentage: 100 - newPercentage * variants.length,
      isControl: false,
    });
    setVariants(updatedVariants);
  }, [variants]);

  const removeVariant = useCallback(
    (index: number) => {
      if (variants.length <= 2) return;
      const updated = variants.filter((_, i) => i !== index);
      // Redistribute percentages
      const each = Math.floor(100 / updated.length);
      const rest = 100 - each * updated.length;
      setVariants(
        updated.map((v, i) => ({
          ...v,
          trafficPercentage: each + (i === 0 ? rest : 0),
        })),
      );
    },
    [variants],
  );

  const updateVariant = useCallback(
    (index: number, field: keyof VariantFormData, value: unknown) => {
      const updated = [...variants];
      (updated[index] as any)[field] = value;
      setVariants(updated);
    },
    [variants],
  );

  const handleTrafficChange = useCallback(
    (index: number, value: number) => {
      const updated = [...variants];
      updated[index].trafficPercentage = value;

      // Distribute remaining among other variants
      const remaining = 100 - value;
      const othersCount = updated.length - 1;
      if (othersCount > 0) {
        const each = Math.floor(remaining / othersCount);
        let distributed = 0;
        updated.forEach((v, i) => {
          if (i !== index) {
            v.trafficPercentage = each;
            distributed += each;
          }
        });
        // Fix rounding
        const diff = remaining - distributed;
        if (diff !== 0) {
          const firstOther = updated.findIndex((_, i) => i !== index);
          if (firstOther >= 0) updated[firstOther].trafficPercentage += diff;
        }
      }

      setVariants(updated);
    },
    [variants],
  );

  const totalPercentage = variants.reduce((s, v) => s + v.trafficPercentage, 0);

  const handleSubmit = useCallback(async () => {
    if (!name.trim() || !agentId) return;

    const data: CreateExperimentData = {
      name: name.trim(),
      description: description.trim() || undefined,
      agentId,
      trafficSplitMethod: splitMethod,
      variants: variants.map((v) => ({
        name: v.name,
        promptText: v.promptText,
        trafficPercentage: v.trafficPercentage,
        isControl: v.isControl,
      })),
      successCriteria,
      minimumSampleSize: minSamples,
      confidenceThreshold,
    };

    createMutation.mutate(data, {
      onSuccess: (result) => {
        onSuccess?.(result.id);
      },
    });
  }, [
    name, description, agentId, splitMethod, variants,
    successCriteria, minSamples, confidenceThreshold,
    createMutation, onSuccess,
  ]);

  return (
    <div className="space-y-6 max-w-3xl">
      {/* Basic Info */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Experiment Details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label>Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., Greeting prompt optimization"
              className="mt-1"
            />
          </div>
          <div>
            <Label>Description (optional)</Label>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What are you testing?"
              className="mt-1"
            />
          </div>
          <div>
            <Label>Agent</Label>
            <Select value={agentId} onValueChange={setAgentId}>
              <SelectTrigger className="mt-1">
                <SelectValue placeholder="Select an agent" />
              </SelectTrigger>
              <SelectContent>
                {agents.map((a) => (
                  <SelectItem key={a.id} value={a.id}>
                    {a.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Variants */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">Variants</CardTitle>
          <Button variant="outline" size="sm" onClick={addVariant}>
            <Plus className="w-3.5 h-3.5 mr-1" />
            Add Variant
          </Button>
        </CardHeader>
        <CardContent className="space-y-4">
          {variants.map((variant, i) => (
            <div key={i} className="border rounded-lg p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <GripVertical className="w-4 h-4 text-muted-foreground" />
                  <Input
                    value={variant.name}
                    onChange={(e) => updateVariant(i, 'name', e.target.value)}
                    className="h-8 w-48 text-sm font-medium"
                  />
                  {variant.isControl && (
                    <Badge variant="outline" className="text-[10px]">
                      Control
                    </Badge>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {!variant.isControl && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => {
                        setVariants(
                          variants.map((v, j) => ({
                            ...v,
                            isControl: j === i,
                          })),
                        );
                      }}
                    >
                      Set as Control
                    </Button>
                  )}
                  {variants.length > 2 && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0"
                      onClick={() => removeVariant(i)}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </Button>
                  )}
                </div>
              </div>

              <div>
                <Label className="text-xs">Prompt Text</Label>
                <Textarea
                  value={variant.promptText}
                  onChange={(e) => updateVariant(i, 'promptText', e.target.value)}
                  placeholder="Enter the system prompt for this variant..."
                  className="mt-1 text-sm min-h-[100px] font-mono"
                />
              </div>

              <div>
                <Label className="text-xs">
                  Traffic Split: {variant.trafficPercentage}%
                </Label>
                <input
                  type="range"
                  min={5}
                  max={95}
                  value={variant.trafficPercentage}
                  onChange={(e) => handleTrafficChange(i, parseInt(e.target.value))}
                  className="w-full mt-1"
                />
              </div>
            </div>
          ))}

          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">
              Total traffic: {totalPercentage}%
            </span>
            {Math.abs(totalPercentage - 100) > 0.1 && (
              <span className="text-red-600">Must equal 100%</span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Settings */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Experiment Settings</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <Label>Traffic Split Method</Label>
              <Select value={splitMethod} onValueChange={setSplitMethod}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="PERCENTAGE">Percentage</SelectItem>
                  <SelectItem value="ROUND_ROBIN">Round Robin</SelectItem>
                  <SelectItem value="STRATIFIED">Stratified</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Success Criteria</Label>
              <Select value={successCriteria} onValueChange={setSuccessCriteria}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="success_rate">Success Rate</SelectItem>
                  <SelectItem value="conversion_rate">Conversion Rate</SelectItem>
                  <SelectItem value="avg_duration">Average Duration</SelectItem>
                  <SelectItem value="avg_sentiment">Average Sentiment</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Minimum Sample Size (per variant)</Label>
              <Input
                type="number"
                value={minSamples}
                onChange={(e) => setMinSamples(parseInt(e.target.value) || 100)}
                min={10}
                className="mt-1"
              />
            </div>
            <div>
              <Label>Confidence Threshold</Label>
              <Select
                value={String(confidenceThreshold)}
                onValueChange={(v) => setConfidenceThreshold(parseFloat(v))}
              >
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="0.9">90%</SelectItem>
                  <SelectItem value="0.95">95%</SelectItem>
                  <SelectItem value="0.99">99%</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Actions */}
      <div className="flex items-center justify-end gap-3">
        {onCancel && (
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button
          onClick={handleSubmit}
          disabled={
            !name.trim() ||
            !agentId ||
            Math.abs(totalPercentage - 100) > 0.1 ||
            createMutation.isPending
          }
        >
          {createMutation.isPending ? 'Creating...' : 'Create Experiment'}
        </Button>
      </div>
    </div>
  );
}
