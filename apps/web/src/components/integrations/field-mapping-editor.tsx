'use client';

import { useState, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  ArrowRight,
  Plus,
  Trash2,
  GripVertical,
  Save,
  RotateCcw,
  TestTube,
  Loader2,
  CheckCircle,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------- Types ----------

interface FieldMapping {
  id: string;
  sourceField: string;
  destinationField: string;
  direction: 'inbound' | 'outbound' | 'bidirectional';
  transform?: string;
  required: boolean;
}

interface FieldOption {
  name: string;
  label: string;
  type: string;
}

interface FieldMappingEditorProps {
  mappings: FieldMapping[];
  sourceFields: FieldOption[];
  destinationFields: FieldOption[];
  sourceLabel?: string;
  destinationLabel?: string;
  onSave: (mappings: FieldMapping[]) => Promise<void>;
  onTest?: (mappings: FieldMapping[]) => Promise<{ success: boolean; results: Record<string, unknown>[] }>;
  className?: string;
}

const TRANSFORM_OPTIONS = [
  { value: 'none', label: 'None' },
  { value: 'uppercase', label: 'UPPERCASE' },
  { value: 'lowercase', label: 'lowercase' },
  { value: 'capitalize', label: 'Capitalize' },
  { value: 'format_phone', label: 'Format Phone' },
  { value: 'format_date', label: 'Format Date' },
];

const DIRECTION_OPTIONS = [
  { value: 'bidirectional', label: 'Both ways', icon: '↔' },
  { value: 'inbound', label: 'CRM to App', icon: '←' },
  { value: 'outbound', label: 'App to CRM', icon: '→' },
];

// ---------- Component ----------

export function FieldMappingEditor({
  mappings: initialMappings,
  sourceFields,
  destinationFields,
  sourceLabel = 'CRM Field',
  destinationLabel = 'Voice AI Field',
  onSave,
  onTest,
  className,
}: FieldMappingEditorProps) {
  const [mappings, setMappings] = useState<FieldMapping[]>(initialMappings);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [testResults, setTestResults] = useState<Record<string, unknown>[] | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);

  const addMapping = useCallback(() => {
    const newMapping: FieldMapping = {
      id: `mapping-${Date.now()}-${Math.random().toString(36).substring(2)}`,
      sourceField: '',
      destinationField: '',
      direction: 'bidirectional',
      transform: 'none',
      required: false,
    };
    setMappings((prev) => [...prev, newMapping]);
    setHasChanges(true);
  }, []);

  const removeMapping = useCallback((id: string) => {
    setMappings((prev) => prev.filter((m) => m.id !== id));
    setHasChanges(true);
  }, []);

  const updateMapping = useCallback(
    (id: string, field: keyof FieldMapping, value: unknown) => {
      setMappings((prev) =>
        prev.map((m) => (m.id === id ? { ...m, [field]: value } : m)),
      );
      setHasChanges(true);
    },
    [],
  );

  const handleSave = useCallback(async () => {
    // Validate: ensure all mappings have both fields
    const invalid = mappings.find((m) => !m.sourceField || !m.destinationField);
    if (invalid) {
      alert('Please fill in all source and destination fields before saving.');
      return;
    }

    setIsSaving(true);
    try {
      await onSave(mappings);
      setHasChanges(false);
    } catch (error) {
      console.error('Save failed:', error);
    } finally {
      setIsSaving(false);
    }
  }, [mappings, onSave]);

  const handleTest = useCallback(async () => {
    if (!onTest) return;

    setIsTesting(true);
    try {
      const result = await onTest(mappings);
      setTestResults(result.results);
    } catch (error) {
      console.error('Test failed:', error);
    } finally {
      setIsTesting(false);
    }
  }, [mappings, onTest]);

  const handleReset = useCallback(() => {
    setMappings(initialMappings);
    setHasChanges(false);
    setTestResults(null);
  }, [initialMappings]);

  const handleDragStart = useCallback((index: number) => {
    setDraggedIndex(index);
  }, []);

  const handleDragOver = useCallback(
    (e: React.DragEvent, index: number) => {
      e.preventDefault();
      if (draggedIndex === null || draggedIndex === index) return;

      setMappings((prev) => {
        const updated = [...prev];
        const [removed] = updated.splice(draggedIndex, 1);
        updated.splice(index, 0, removed);
        return updated;
      });
      setDraggedIndex(index);
      setHasChanges(true);
    },
    [draggedIndex],
  );

  const handleDragEnd = useCallback(() => {
    setDraggedIndex(null);
  }, []);

  return (
    <Card className={className}>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-lg">Field Mappings</CardTitle>
            <CardDescription>
              Map fields between your CRM and Voice AI. Drag to reorder.
            </CardDescription>
          </div>
          {hasChanges && (
            <Badge variant="warning" className="text-xs">
              Unsaved changes
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Header */}
        <div className="grid grid-cols-[32px_1fr_32px_1fr_120px_80px_32px] gap-2 items-center text-xs font-medium text-muted-foreground px-1">
          <div />
          <div>{sourceLabel}</div>
          <div />
          <div>{destinationLabel}</div>
          <div>Transform</div>
          <div>Direction</div>
          <div />
        </div>

        {/* Mappings */}
        <div className="space-y-2">
          {mappings.map((mapping, index) => (
            <div
              key={mapping.id}
              className={cn(
                'grid grid-cols-[32px_1fr_32px_1fr_120px_80px_32px] gap-2 items-center rounded-lg border p-2 transition-colors',
                draggedIndex === index && 'opacity-50 border-primary',
              )}
              draggable
              onDragStart={() => handleDragStart(index)}
              onDragOver={(e) => handleDragOver(e, index)}
              onDragEnd={handleDragEnd}
            >
              {/* Drag Handle */}
              <div className="cursor-grab flex justify-center">
                <GripVertical className="h-4 w-4 text-muted-foreground" />
              </div>

              {/* Source Field */}
              <Select
                value={mapping.sourceField}
                onValueChange={(value) =>
                  updateMapping(mapping.id, 'sourceField', value)
                }
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue placeholder="Select field..." />
                </SelectTrigger>
                <SelectContent>
                  {sourceFields.map((field) => (
                    <SelectItem key={field.name} value={field.name}>
                      <div className="flex items-center gap-2">
                        <span>{field.label}</span>
                        <span className="text-muted-foreground text-[10px]">
                          ({field.type})
                        </span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Arrow */}
              <div className="flex justify-center">
                <ArrowRight className="h-4 w-4 text-muted-foreground" />
              </div>

              {/* Destination Field */}
              <Select
                value={mapping.destinationField}
                onValueChange={(value) =>
                  updateMapping(mapping.id, 'destinationField', value)
                }
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue placeholder="Select field..." />
                </SelectTrigger>
                <SelectContent>
                  {destinationFields.map((field) => (
                    <SelectItem key={field.name} value={field.name}>
                      <div className="flex items-center gap-2">
                        <span>{field.label}</span>
                        <span className="text-muted-foreground text-[10px]">
                          ({field.type})
                        </span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Transform */}
              <Select
                value={mapping.transform || 'none'}
                onValueChange={(value) =>
                  updateMapping(mapping.id, 'transform', value)
                }
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TRANSFORM_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Direction */}
              <Select
                value={mapping.direction}
                onValueChange={(value) =>
                  updateMapping(
                    mapping.id,
                    'direction',
                    value as FieldMapping['direction'],
                  )
                }
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DIRECTION_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.icon} {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* Remove */}
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => removeMapping(mapping.id)}
              >
                <Trash2 className="h-3.5 w-3.5 text-muted-foreground hover:text-destructive" />
              </Button>
            </div>
          ))}
        </div>

        {/* Add Mapping */}
        <Button variant="outline" size="sm" onClick={addMapping} className="w-full">
          <Plus className="mr-2 h-4 w-4" />
          Add Field Mapping
        </Button>

        {/* Test Results */}
        {testResults && (
          <div className="rounded-lg border bg-muted/50 p-4 space-y-2">
            <div className="flex items-center gap-2">
              <CheckCircle className="h-4 w-4 text-green-600" />
              <span className="text-sm font-medium">Test Results</span>
            </div>
            <pre className="text-xs bg-background rounded p-3 overflow-auto max-h-40">
              {JSON.stringify(testResults, null, 2)}
            </pre>
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center justify-between pt-2 border-t">
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleReset}
              disabled={!hasChanges}
            >
              <RotateCcw className="mr-2 h-4 w-4" />
              Reset
            </Button>
            {onTest && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleTest}
                disabled={isTesting || mappings.length === 0}
              >
                {isTesting ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <TestTube className="mr-2 h-4 w-4" />
                )}
                Test Mapping
              </Button>
            )}
          </div>
          <Button size="sm" onClick={handleSave} disabled={isSaving || !hasChanges}>
            {isSaving ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Save className="mr-2 h-4 w-4" />
            )}
            Save Mappings
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
