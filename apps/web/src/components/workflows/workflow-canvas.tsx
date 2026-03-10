// NOTE: Required packages - add to package.json:
//   "reactflow": "^11.11.0"
//   "dagre": "^0.8.5"
//   "@types/dagre": "^0.7.52"

'use client';

import { useCallback, useRef, useMemo, useEffect, useState } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Panel,
  useReactFlow,
  addEdge,
  type Connection,
  type Edge,
  type Node,
  type NodeTypes,
  type EdgeTypes,
  type OnNodesChange,
  type OnEdgesChange,
  applyNodeChanges,
  applyEdgeChanges,
  type ReactFlowInstance,
  MarkerType,
  SelectionMode,
} from 'reactflow';
import dagre from 'dagre';
import { WorkflowNodeComponent } from './workflow-node';
import { WorkflowEdgeComponent } from './workflow-edge';
import { NodePalette } from './node-palette';
import { NodeConfigPanel } from './node-config-panel';
import { WorkflowToolbar } from './workflow-toolbar';
import { WorkflowSimulatorPanel } from './workflow-simulator-panel';
import {
  useWorkflowStore,
  WorkflowNodeType,
  type WorkflowNode,
  type WorkflowEdge,
} from '@/stores/workflow-store';
import api from '@/lib/api';

// ============================================================================
// Node/Edge Type Registration
// ============================================================================

const nodeTypes: NodeTypes = {
  workflowNode: WorkflowNodeComponent as any,
};

const edgeTypes: EdgeTypes = {
  workflowEdge: WorkflowEdgeComponent as any,
};

// ============================================================================
// Auto-layout with Dagre
// ============================================================================

function getLayoutedElements(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
  direction: 'TB' | 'LR' = 'TB',
): { nodes: WorkflowNode[]; edges: WorkflowEdge[] } {
  const dagreGraph = new dagre.graphlib.Graph();
  dagreGraph.setDefaultEdgeLabel(() => ({}));
  dagreGraph.setGraph({ rankdir: direction, nodesep: 80, ranksep: 100 });

  const nodeWidth = 220;
  const nodeHeight = 80;

  nodes.forEach((node) => {
    dagreGraph.setNode(node.id, { width: nodeWidth, height: nodeHeight });
  });

  edges.forEach((edge) => {
    dagreGraph.setEdge(edge.source, edge.target);
  });

  dagre.layout(dagreGraph);

  const layoutedNodes = nodes.map((node) => {
    const nodeWithPosition = dagreGraph.node(node.id);
    return {
      ...node,
      position: {
        x: nodeWithPosition.x - nodeWidth / 2,
        y: nodeWithPosition.y - nodeHeight / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}

// ============================================================================
// Canvas Component
// ============================================================================

export function WorkflowCanvas() {
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = useState<ReactFlowInstance | null>(null);
  const [showSimulator, setShowSimulator] = useState(false);

  const {
    nodes,
    edges,
    meta,
    selectedNodeIds,
    setNodes,
    setEdges,
    addNode,
    addEdge: addStoreEdge,
    removeNodes,
    removeEdges,
    selectNodes,
    selectEdges,
    clearSelection,
    pushHistory,
    copySelected,
    pasteClipboard,
    setViewport,
    setIsSaving,
    setIsDirty,
    setIsSimulating,
    undo,
    redo,
    loadWorkflow,
  } = useWorkflowStore();

  // Convert store nodes to ReactFlow nodes
  const rfNodes: Node[] = useMemo(
    () =>
      nodes.map((n) => ({
        id: n.id,
        type: 'workflowNode',
        position: n.position,
        data: n.data,
        selected: selectedNodeIds.includes(n.id),
      })),
    [nodes, selectedNodeIds],
  );

  // Convert store edges to ReactFlow edges
  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.sourceHandle,
        targetHandle: e.targetHandle,
        type: 'workflowEdge',
        label: e.label,
        data: e.data,
        animated: e.animated,
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      })),
    [edges],
  );

  // --------------------------------------------------------------------------
  // Node/Edge Change Handlers
  // --------------------------------------------------------------------------

  const onNodesChange: OnNodesChange = useCallback(
    (changes) => {
      const updated = applyNodeChanges(changes, rfNodes);
      setNodes(
        updated.map((n) => ({
          id: n.id,
          type: n.type || 'workflowNode',
          position: n.position!,
          data: n.data as any,
          selected: n.selected,
          width: n.width ?? undefined,
          height: n.height ?? undefined,
        })),
      );

      // Track selection
      const selectedIds = updated.filter((n) => n.selected).map((n) => n.id);
      selectNodes(selectedIds);
    },
    [rfNodes, setNodes, selectNodes],
  );

  const onEdgesChange: OnEdgesChange = useCallback(
    (changes) => {
      const updated = applyEdgeChanges(changes, rfEdges);
      setEdges(
        updated.map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          sourceHandle: e.sourceHandle || undefined,
          targetHandle: e.targetHandle || undefined,
          label: e.label as string | undefined,
          type: e.type,
          animated: e.animated,
          data: e.data,
        })),
      );
    },
    [rfEdges, setEdges],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      const edgeId = `edge_${connection.source}_${connection.target}_${Date.now()}`;
      addStoreEdge({
        id: edgeId,
        source: connection.source,
        target: connection.target,
        sourceHandle: connection.sourceHandle || undefined,
        targetHandle: connection.targetHandle || undefined,
        type: 'workflowEdge',
        animated: false,
      });
    },
    [addStoreEdge],
  );

  // --------------------------------------------------------------------------
  // Drag and Drop
  // --------------------------------------------------------------------------

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      const dataStr = event.dataTransfer.getData('application/workflow-node');
      if (!dataStr || !reactFlowInstance) return;

      const { type, label } = JSON.parse(dataStr);

      const position = reactFlowInstance.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      const newNode: WorkflowNode = {
        id: `${type.toLowerCase()}_${Date.now()}_${Math.random().toString(36).slice(2, 5)}`,
        type: 'workflowNode',
        position,
        data: {
          label,
          type,
          prompt: '',
          config: {},
        },
      };

      addNode(newNode);
    },
    [reactFlowInstance, addNode],
  );

  // --------------------------------------------------------------------------
  // Keyboard Shortcuts
  // --------------------------------------------------------------------------

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if focus is in an input
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
        e.preventDefault();
        undo();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && e.shiftKey) {
        e.preventDefault();
        redo();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
        e.preventDefault();
        copySelected();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
        e.preventDefault();
        pasteClipboard();
      }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        const state = useWorkflowStore.getState();
        if (state.selectedNodeIds.length > 0) {
          removeNodes(state.selectedNodeIds);
        }
        if (state.selectedEdgeIds.length > 0) {
          removeEdges(state.selectedEdgeIds);
        }
      }
      if (e.key === 'Escape') {
        clearSelection();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [undo, redo, copySelected, pasteClipboard, removeNodes, removeEdges, clearSelection]);

  // Handle custom events from nodes
  useEffect(() => {
    const handleDeleteNode = (e: Event) => {
      const nodeId = (e as CustomEvent).detail.nodeId;
      removeNodes([nodeId]);
    };
    const handleDeleteEdge = (e: Event) => {
      const edgeId = (e as CustomEvent).detail.edgeId;
      removeEdges([edgeId]);
    };
    const handleEditNode = (e: Event) => {
      const nodeId = (e as CustomEvent).detail.nodeId;
      selectNodes([nodeId]);
    };

    window.addEventListener('workflow:delete-node', handleDeleteNode);
    window.addEventListener('workflow:delete-edge', handleDeleteEdge);
    window.addEventListener('workflow:edit-node', handleEditNode);

    return () => {
      window.removeEventListener('workflow:delete-node', handleDeleteNode);
      window.removeEventListener('workflow:delete-edge', handleDeleteEdge);
      window.removeEventListener('workflow:edit-node', handleEditNode);
    };
  }, [removeNodes, removeEdges, selectNodes]);

  // --------------------------------------------------------------------------
  // Toolbar Actions
  // --------------------------------------------------------------------------

  const handleSave = useCallback(async () => {
    setIsSaving(true);
    try {
      const state = useWorkflowStore.getState();
      const payload = {
        name: state.meta.name,
        description: state.meta.description,
        nodes: state.nodes,
        edges: state.edges,
        agentId: state.meta.agentId,
        isActive: state.meta.isActive,
      };

      if (state.meta.id) {
        await api.put(`/workflows/${state.meta.id}`, payload);
      } else {
        const { data } = await api.post('/workflows', payload);
        useWorkflowStore.getState().setMeta({ id: data.id });
      }

      setIsDirty(false);
    } catch (error) {
      console.error('Save failed:', error);
    } finally {
      setIsSaving(false);
    }
  }, [setIsSaving, setIsDirty]);

  const handlePublish = useCallback(async () => {
    await handleSave();
    const state = useWorkflowStore.getState();
    if (state.meta.id) {
      try {
        await api.post(`/workflows/${state.meta.id}/versions`);
      } catch (error) {
        console.error('Publish failed:', error);
      }
    }
  }, [handleSave]);

  const handleAutoLayout = useCallback(() => {
    pushHistory();
    const { nodes: layoutedNodes, edges: layoutedEdges } = getLayoutedElements(
      nodes,
      edges,
      'TB',
    );
    setNodes(layoutedNodes);
    setEdges(layoutedEdges);
    setTimeout(() => reactFlowInstance?.fitView({ padding: 0.2 }), 50);
  }, [nodes, edges, setNodes, setEdges, pushHistory, reactFlowInstance]);

  const handleZoomIn = useCallback(() => {
    reactFlowInstance?.zoomIn();
  }, [reactFlowInstance]);

  const handleZoomOut = useCallback(() => {
    reactFlowInstance?.zoomOut();
  }, [reactFlowInstance]);

  const handleFitView = useCallback(() => {
    reactFlowInstance?.fitView({ padding: 0.2 });
  }, [reactFlowInstance]);

  const handleExport = useCallback(() => {
    const state = useWorkflowStore.getState();
    const data = JSON.stringify(state.getExportData(), null, 2);
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `workflow-${state.meta.name.replace(/\s+/g, '-').toLowerCase()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, []);

  const handleImport = useCallback(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        try {
          const data = JSON.parse(ev.target?.result as string);
          if (data.nodes && data.edges) {
            loadWorkflow({
              nodes: data.nodes,
              edges: data.edges,
              meta: data.meta || { name: file.name.replace('.json', '') },
            });
            setTimeout(() => reactFlowInstance?.fitView({ padding: 0.2 }), 100);
          }
        } catch {
          console.error('Invalid workflow JSON');
        }
      };
      reader.readAsText(file);
    };
    input.click();
  }, [loadWorkflow, reactFlowInstance]);

  const handleSimulate = useCallback(() => {
    setShowSimulator(true);
    setIsSimulating(true);
  }, [setIsSimulating]);

  // --------------------------------------------------------------------------
  // Render
  // --------------------------------------------------------------------------

  return (
    <div className="flex flex-col h-full">
      <WorkflowToolbar
        onSave={handleSave}
        onPublish={handlePublish}
        onSimulate={handleSimulate}
        onAutoLayout={handleAutoLayout}
        onZoomIn={handleZoomIn}
        onZoomOut={handleZoomOut}
        onFitView={handleFitView}
        onExport={handleExport}
        onImport={handleImport}
      />

      <div className="flex flex-1 overflow-hidden">
        {/* Node Palette */}
        <NodePalette />

        {/* Canvas */}
        <div className="flex-1" ref={reactFlowWrapper}>
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onInit={setReactFlowInstance}
            onDragOver={onDragOver}
            onDrop={onDrop}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            snapToGrid
            snapGrid={[16, 16]}
            selectionMode={SelectionMode.Partial}
            multiSelectionKeyCode="Shift"
            deleteKeyCode={null} // Handle delete manually
            defaultEdgeOptions={{
              type: 'workflowEdge',
              markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
            }}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={16} size={1} />
            <Controls showInteractive={false} />
            <MiniMap
              nodeStrokeWidth={3}
              zoomable
              pannable
              className="!bg-card !border !rounded-lg"
            />
          </ReactFlow>
        </div>

        {/* Config Panel or Simulator */}
        {showSimulator ? (
          <WorkflowSimulatorPanel
            onClose={() => {
              setShowSimulator(false);
              setIsSimulating(false);
            }}
            workflowId={meta.id}
          />
        ) : (
          selectedNodeIds.length > 0 && <NodeConfigPanel />
        )}
      </div>
    </div>
  );
}
