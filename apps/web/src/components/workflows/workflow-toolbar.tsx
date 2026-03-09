'use client';

import { useCallback } from 'react';
import {
  Save,
  Upload,
  Play,
  Undo2,
  Redo2,
  ZoomIn,
  ZoomOut,
  Maximize2,
  LayoutGrid,
  Download,
  FileJson,
  FlaskConical,
  Eye,
  Loader2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { useWorkflowStore } from '@/stores/workflow-store';
import { cn } from '@/lib/utils';

interface WorkflowToolbarProps {
  onSave: () => void;
  onPublish: () => void;
  onSimulate: () => void;
  onAutoLayout: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitView: () => void;
  onExport: () => void;
  onImport: () => void;
}

export function WorkflowToolbar({
  onSave,
  onPublish,
  onSimulate,
  onAutoLayout,
  onZoomIn,
  onZoomOut,
  onFitView,
  onExport,
  onImport,
}: WorkflowToolbarProps) {
  const { isDirty, isSaving, isSimulating, undo, redo, history, historyIndex, meta } =
    useWorkflowStore();

  const canUndo = historyIndex >= 0;
  const canRedo = historyIndex < history.length - 1;

  return (
    <div className="flex items-center justify-between px-3 py-1.5 border-b bg-card">
      {/* Left: workflow info */}
      <div className="flex items-center gap-3">
        <div>
          <h2 className="text-sm font-semibold leading-none">{meta.name}</h2>
          {meta.version && (
            <p className="text-[10px] text-muted-foreground mt-0.5">
              v{meta.version}
              {isDirty && ' (unsaved changes)'}
            </p>
          )}
        </div>
      </div>

      {/* Center: editing tools */}
      <div className="flex items-center gap-0.5">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={undo}
          disabled={!canUndo}
          title="Undo (Ctrl+Z)"
        >
          <Undo2 className="w-3.5 h-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={redo}
          disabled={!canRedo}
          title="Redo (Ctrl+Shift+Z)"
        >
          <Redo2 className="w-3.5 h-3.5" />
        </Button>

        <Separator orientation="vertical" className="mx-1.5 h-5" />

        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={onZoomIn}
          title="Zoom In"
        >
          <ZoomIn className="w-3.5 h-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={onZoomOut}
          title="Zoom Out"
        >
          <ZoomOut className="w-3.5 h-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={onFitView}
          title="Fit View"
        >
          <Maximize2 className="w-3.5 h-3.5" />
        </Button>

        <Separator orientation="vertical" className="mx-1.5 h-5" />

        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={onAutoLayout}
          title="Auto Layout"
        >
          <LayoutGrid className="w-3.5 h-3.5 mr-1" />
          Layout
        </Button>

        <Separator orientation="vertical" className="mx-1.5 h-5" />

        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={onExport}
          title="Export JSON"
        >
          <Download className="w-3.5 h-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={onImport}
          title="Import JSON"
        >
          <FileJson className="w-3.5 h-3.5" />
        </Button>
      </div>

      {/* Right: main actions */}
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={onSimulate}
          disabled={isSimulating}
        >
          {isSimulating ? (
            <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
          ) : (
            <FlaskConical className="w-3.5 h-3.5 mr-1" />
          )}
          Test
        </Button>

        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={onSave}
          disabled={isSaving || !isDirty}
        >
          {isSaving ? (
            <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
          ) : (
            <Save className="w-3.5 h-3.5 mr-1" />
          )}
          Save
        </Button>

        <Button size="sm" className="h-7 text-xs" onClick={onPublish}>
          <Upload className="w-3.5 h-3.5 mr-1" />
          Publish
        </Button>
      </div>
    </div>
  );
}
