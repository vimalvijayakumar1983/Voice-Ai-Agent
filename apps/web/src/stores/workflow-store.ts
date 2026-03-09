import { create } from 'zustand';

// ============================================================================
// Types
// ============================================================================

export enum WorkflowNodeType {
  GREETING = 'GREETING',
  CONSENT_CAPTURE = 'CONSENT_CAPTURE',
  VERIFICATION = 'VERIFICATION',
  QUALIFICATION = 'QUALIFICATION',
  INFO_CAPTURE = 'INFO_CAPTURE',
  LOOKUP = 'LOOKUP',
  SUMMARIZE = 'SUMMARIZE',
  OFFER_OPTIONS = 'OFFER_OPTIONS',
  BOOK_APPOINTMENT = 'BOOK_APPOINTMENT',
  COLLECT_PAYMENT = 'COLLECT_PAYMENT',
  ESCALATE_HUMAN = 'ESCALATE_HUMAN',
  SEND_MESSAGE = 'SEND_MESSAGE',
  END_CALL = 'END_CALL',
  RETRY_LATER = 'RETRY_LATER',
  CONDITIONAL = 'CONDITIONAL',
  WAIT = 'WAIT',
  API_CALL = 'API_CALL',
  SET_VARIABLE = 'SET_VARIABLE',
}

export interface WorkflowNodeData {
  label: string;
  type: WorkflowNodeType;
  prompt?: string;
  config?: Record<string, unknown>;
  fallbackNodeId?: string;
  timeoutSeconds?: number;
  retryCount?: number;
  [key: string]: unknown;
}

export interface WorkflowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: WorkflowNodeData;
  selected?: boolean;
  width?: number;
  height?: number;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
  label?: string;
  type?: string;
  animated?: boolean;
  data?: {
    conditions?: {
      expression?: string;
      operator?: string;
      field?: string;
      value?: unknown;
    };
  };
}

interface HistoryEntry {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowMeta {
  id?: string;
  name: string;
  description?: string;
  agentId?: string;
  version?: number;
  isActive?: boolean;
}

// ============================================================================
// Store
// ============================================================================

interface WorkflowState {
  // Core state
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  meta: WorkflowMeta;
  selectedNodeIds: string[];
  selectedEdgeIds: string[];

  // Undo/redo
  history: HistoryEntry[];
  historyIndex: number;

  // Clipboard
  clipboard: { nodes: WorkflowNode[]; edges: WorkflowEdge[] } | null;

  // Canvas
  viewport: { x: number; y: number; zoom: number };

  // State tracking
  isDirty: boolean;
  isSaving: boolean;
  isSimulating: boolean;

  // Actions
  setNodes: (nodes: WorkflowNode[]) => void;
  setEdges: (edges: WorkflowEdge[]) => void;
  setMeta: (meta: Partial<WorkflowMeta>) => void;

  addNode: (node: WorkflowNode) => void;
  updateNode: (id: string, data: Partial<WorkflowNodeData>) => void;
  removeNodes: (ids: string[]) => void;
  moveNode: (id: string, position: { x: number; y: number }) => void;

  addEdge: (edge: WorkflowEdge) => void;
  updateEdge: (id: string, data: Partial<WorkflowEdge>) => void;
  removeEdges: (ids: string[]) => void;

  selectNodes: (ids: string[]) => void;
  selectEdges: (ids: string[]) => void;
  clearSelection: () => void;

  undo: () => void;
  redo: () => void;
  pushHistory: () => void;

  copySelected: () => void;
  pasteClipboard: (offset?: { x: number; y: number }) => void;

  setViewport: (viewport: { x: number; y: number; zoom: number }) => void;
  setIsDirty: (dirty: boolean) => void;
  setIsSaving: (saving: boolean) => void;
  setIsSimulating: (simulating: boolean) => void;

  loadWorkflow: (data: {
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
    meta: WorkflowMeta;
  }) => void;
  resetWorkflow: () => void;
  getExportData: () => { nodes: WorkflowNode[]; edges: WorkflowEdge[]; meta: WorkflowMeta };
}

const MAX_HISTORY = 50;

export const useWorkflowStore = create<WorkflowState>()((set, get) => ({
  nodes: [],
  edges: [],
  meta: { name: 'Untitled Workflow' },
  selectedNodeIds: [],
  selectedEdgeIds: [],
  history: [],
  historyIndex: -1,
  clipboard: null,
  viewport: { x: 0, y: 0, zoom: 1 },
  isDirty: false,
  isSaving: false,
  isSimulating: false,

  // --------------------------------------------------------------------------
  // Core Setters
  // --------------------------------------------------------------------------

  setNodes: (nodes) => set({ nodes, isDirty: true }),
  setEdges: (edges) => set({ edges, isDirty: true }),
  setMeta: (meta) =>
    set((state) => ({ meta: { ...state.meta, ...meta }, isDirty: true })),

  // --------------------------------------------------------------------------
  // Node Operations
  // --------------------------------------------------------------------------

  addNode: (node) => {
    const state = get();
    state.pushHistory();
    set({ nodes: [...state.nodes, node], isDirty: true });
  },

  updateNode: (id, data) => {
    const state = get();
    state.pushHistory();
    set({
      nodes: state.nodes.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, ...data } } : n,
      ),
      isDirty: true,
    });
  },

  removeNodes: (ids) => {
    const state = get();
    state.pushHistory();
    const idSet = new Set(ids);
    set({
      nodes: state.nodes.filter((n) => !idSet.has(n.id)),
      edges: state.edges.filter(
        (e) => !idSet.has(e.source) && !idSet.has(e.target),
      ),
      selectedNodeIds: state.selectedNodeIds.filter((id) => !idSet.has(id)),
      isDirty: true,
    });
  },

  moveNode: (id, position) => {
    set((state) => ({
      nodes: state.nodes.map((n) =>
        n.id === id ? { ...n, position } : n,
      ),
      isDirty: true,
    }));
  },

  // --------------------------------------------------------------------------
  // Edge Operations
  // --------------------------------------------------------------------------

  addEdge: (edge) => {
    const state = get();
    // Prevent duplicate edges
    const exists = state.edges.some(
      (e) => e.source === edge.source && e.target === edge.target,
    );
    if (exists) return;

    state.pushHistory();
    set({ edges: [...state.edges, edge], isDirty: true });
  },

  updateEdge: (id, data) => {
    const state = get();
    state.pushHistory();
    set({
      edges: state.edges.map((e) =>
        e.id === id ? { ...e, ...data } : e,
      ),
      isDirty: true,
    });
  },

  removeEdges: (ids) => {
    const state = get();
    state.pushHistory();
    const idSet = new Set(ids);
    set({
      edges: state.edges.filter((e) => !idSet.has(e.id)),
      selectedEdgeIds: state.selectedEdgeIds.filter((id) => !idSet.has(id)),
      isDirty: true,
    });
  },

  // --------------------------------------------------------------------------
  // Selection
  // --------------------------------------------------------------------------

  selectNodes: (ids) => set({ selectedNodeIds: ids }),
  selectEdges: (ids) => set({ selectedEdgeIds: ids }),
  clearSelection: () => set({ selectedNodeIds: [], selectedEdgeIds: [] }),

  // --------------------------------------------------------------------------
  // Undo / Redo
  // --------------------------------------------------------------------------

  pushHistory: () => {
    const state = get();
    const entry: HistoryEntry = {
      nodes: JSON.parse(JSON.stringify(state.nodes)),
      edges: JSON.parse(JSON.stringify(state.edges)),
    };

    // Remove future history if we're not at the end
    const newHistory = state.history.slice(0, state.historyIndex + 1);
    newHistory.push(entry);

    // Limit history size
    if (newHistory.length > MAX_HISTORY) {
      newHistory.shift();
    }

    set({
      history: newHistory,
      historyIndex: newHistory.length - 1,
    });
  },

  undo: () => {
    const state = get();
    if (state.historyIndex < 0) return;

    const entry = state.history[state.historyIndex];
    if (!entry) return;

    set({
      nodes: JSON.parse(JSON.stringify(entry.nodes)),
      edges: JSON.parse(JSON.stringify(entry.edges)),
      historyIndex: state.historyIndex - 1,
      isDirty: true,
    });
  },

  redo: () => {
    const state = get();
    if (state.historyIndex >= state.history.length - 1) return;

    const nextIndex = state.historyIndex + 1;
    const entry = state.history[nextIndex];
    if (!entry) return;

    set({
      nodes: JSON.parse(JSON.stringify(entry.nodes)),
      edges: JSON.parse(JSON.stringify(entry.edges)),
      historyIndex: nextIndex,
      isDirty: true,
    });
  },

  // --------------------------------------------------------------------------
  // Clipboard
  // --------------------------------------------------------------------------

  copySelected: () => {
    const state = get();
    const selectedNodes = state.nodes.filter((n) =>
      state.selectedNodeIds.includes(n.id),
    );
    const nodeIds = new Set(selectedNodes.map((n) => n.id));
    const selectedEdges = state.edges.filter(
      (e) => nodeIds.has(e.source) && nodeIds.has(e.target),
    );

    set({
      clipboard: {
        nodes: JSON.parse(JSON.stringify(selectedNodes)),
        edges: JSON.parse(JSON.stringify(selectedEdges)),
      },
    });
  },

  pasteClipboard: (offset = { x: 50, y: 50 }) => {
    const state = get();
    if (!state.clipboard) return;

    state.pushHistory();

    const idMap = new Map<string, string>();
    const newNodes = state.clipboard.nodes.map((n) => {
      const newId = `${n.id}_copy_${Date.now()}_${Math.random().toString(36).slice(2, 5)}`;
      idMap.set(n.id, newId);
      return {
        ...n,
        id: newId,
        position: {
          x: n.position.x + offset.x,
          y: n.position.y + offset.y,
        },
        selected: true,
      };
    });

    const newEdges = state.clipboard.edges.map((e) => ({
      ...e,
      id: `${e.id}_copy_${Date.now()}`,
      source: idMap.get(e.source) || e.source,
      target: idMap.get(e.target) || e.target,
    }));

    set({
      nodes: [
        ...state.nodes.map((n) => ({ ...n, selected: false })),
        ...newNodes,
      ],
      edges: [...state.edges, ...newEdges],
      selectedNodeIds: newNodes.map((n) => n.id),
      isDirty: true,
    });
  },

  // --------------------------------------------------------------------------
  // Canvas
  // --------------------------------------------------------------------------

  setViewport: (viewport) => set({ viewport }),
  setIsDirty: (isDirty) => set({ isDirty }),
  setIsSaving: (isSaving) => set({ isSaving }),
  setIsSimulating: (isSimulating) => set({ isSimulating }),

  // --------------------------------------------------------------------------
  // Workflow Load / Reset / Export
  // --------------------------------------------------------------------------

  loadWorkflow: (data) =>
    set({
      nodes: data.nodes,
      edges: data.edges,
      meta: data.meta,
      history: [],
      historyIndex: -1,
      selectedNodeIds: [],
      selectedEdgeIds: [],
      isDirty: false,
    }),

  resetWorkflow: () =>
    set({
      nodes: [],
      edges: [],
      meta: { name: 'Untitled Workflow' },
      history: [],
      historyIndex: -1,
      clipboard: null,
      selectedNodeIds: [],
      selectedEdgeIds: [],
      isDirty: false,
    }),

  getExportData: () => {
    const state = get();
    return {
      nodes: state.nodes,
      edges: state.edges,
      meta: state.meta,
    };
  },
}));
