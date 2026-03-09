import { Injectable, Logger } from '@nestjs/common';

// ============================================================================
// Types
// ============================================================================

export interface ValidationResult {
  valid: boolean;
  errors: ValidationError[];
  warnings: ValidationWarning[];
}

export interface ValidationError {
  code: string;
  message: string;
  nodeId?: string;
  edgeId?: string;
  severity: 'error';
}

export interface ValidationWarning {
  code: string;
  message: string;
  nodeId?: string;
  edgeId?: string;
  severity: 'warning';
}

interface WorkflowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
}

interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  conditions?: Record<string, unknown>;
}

// Required config fields per node type
const NODE_CONFIG_REQUIREMENTS: Record<string, string[]> = {
  GREETING: [],
  CONSENT_CAPTURE: [],
  VERIFICATION: [],
  QUALIFICATION: [],
  INFO_CAPTURE: [],
  LOOKUP: ['endpoint'],
  SUMMARIZE: [],
  OFFER_OPTIONS: ['options'],
  BOOK_APPOINTMENT: [],
  COLLECT_PAYMENT: [],
  ESCALATE_HUMAN: [],
  SEND_MESSAGE: ['channel'],
  END_CALL: [],
  RETRY_LATER: [],
  CONDITIONAL: [],
  WAIT: [],
  API_CALL: ['url'],
  SET_VARIABLE: ['assignments'],
};

// Node types that typically require outgoing edges
const NODES_REQUIRING_OUTGOING_EDGES = new Set([
  'GREETING',
  'CONSENT_CAPTURE',
  'VERIFICATION',
  'QUALIFICATION',
  'INFO_CAPTURE',
  'LOOKUP',
  'SUMMARIZE',
  'OFFER_OPTIONS',
  'BOOK_APPOINTMENT',
  'COLLECT_PAYMENT',
  'CONDITIONAL',
  'WAIT',
  'API_CALL',
  'SET_VARIABLE',
]);

// Terminal node types that should not have outgoing edges
const TERMINAL_NODE_TYPES = new Set(['END_CALL', 'RETRY_LATER', 'ESCALATE_HUMAN']);

// ============================================================================
// Service
// ============================================================================

@Injectable()
export class WorkflowValidatorService {
  private readonly logger = new Logger(WorkflowValidatorService.name);

  validate(nodes: WorkflowNode[], edges: WorkflowEdge[]): ValidationResult {
    const errors: ValidationError[] = [];
    const warnings: ValidationWarning[] = [];

    // Structural checks
    this.validateBasicStructure(nodes, edges, errors);
    this.validateNodeIds(nodes, edges, errors);
    this.validateEdgeReferences(nodes, edges, errors);
    this.validateReachability(nodes, edges, errors, warnings);
    this.validateCyclesForInfiniteLoops(nodes, edges, errors, warnings);
    this.validateTerminalNodes(nodes, edges, errors, warnings);
    this.validateNodeConfigurations(nodes, errors, warnings);
    this.validateEdgeConditions(edges, warnings);
    this.validateNodeConnectivity(nodes, edges, warnings);

    return {
      valid: errors.length === 0,
      errors,
      warnings,
    };
  }

  // --------------------------------------------------------------------------
  // Basic Structure
  // --------------------------------------------------------------------------

  private validateBasicStructure(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    errors: ValidationError[],
  ): void {
    if (!nodes || nodes.length === 0) {
      errors.push({
        code: 'EMPTY_WORKFLOW',
        message: 'Workflow must have at least one node',
        severity: 'error',
      });
      return;
    }

    // Must have exactly one start/greeting node
    const startNodes = nodes.filter(
      (n) => n.type === 'start' || n.type === 'GREETING',
    );
    if (startNodes.length === 0) {
      errors.push({
        code: 'NO_START_NODE',
        message: 'Workflow must have at least one start or GREETING node',
        severity: 'error',
      });
    }
    if (startNodes.length > 1) {
      errors.push({
        code: 'MULTIPLE_START_NODES',
        message: `Workflow has ${startNodes.length} start nodes; exactly one is required`,
        severity: 'error',
      });
    }
  }

  // --------------------------------------------------------------------------
  // Unique Node IDs
  // --------------------------------------------------------------------------

  private validateNodeIds(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    errors: ValidationError[],
  ): void {
    const nodeIds = new Set<string>();
    for (const node of nodes) {
      if (!node.id) {
        errors.push({
          code: 'MISSING_NODE_ID',
          message: 'A node is missing an ID',
          severity: 'error',
        });
        continue;
      }
      if (nodeIds.has(node.id)) {
        errors.push({
          code: 'DUPLICATE_NODE_ID',
          message: `Duplicate node ID: ${node.id}`,
          nodeId: node.id,
          severity: 'error',
        });
      }
      nodeIds.add(node.id);
    }

    const edgeIds = new Set<string>();
    for (const edge of edges) {
      if (!edge.id) {
        errors.push({
          code: 'MISSING_EDGE_ID',
          message: 'An edge is missing an ID',
          severity: 'error',
        });
        continue;
      }
      if (edgeIds.has(edge.id)) {
        errors.push({
          code: 'DUPLICATE_EDGE_ID',
          message: `Duplicate edge ID: ${edge.id}`,
          edgeId: edge.id,
          severity: 'error',
        });
      }
      edgeIds.add(edge.id);
    }
  }

  // --------------------------------------------------------------------------
  // Edge References
  // --------------------------------------------------------------------------

  private validateEdgeReferences(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    errors: ValidationError[],
  ): void {
    const nodeIds = new Set(nodes.map((n) => n.id));

    for (const edge of edges) {
      if (!nodeIds.has(edge.source)) {
        errors.push({
          code: 'INVALID_EDGE_SOURCE',
          message: `Edge ${edge.id} references non-existent source node: ${edge.source}`,
          edgeId: edge.id,
          severity: 'error',
        });
      }
      if (!nodeIds.has(edge.target)) {
        errors.push({
          code: 'INVALID_EDGE_TARGET',
          message: `Edge ${edge.id} references non-existent target node: ${edge.target}`,
          edgeId: edge.id,
          severity: 'error',
        });
      }
      // Self-loops
      if (edge.source === edge.target) {
        errors.push({
          code: 'SELF_LOOP',
          message: `Edge ${edge.id} creates a self-loop on node ${edge.source}`,
          edgeId: edge.id,
          nodeId: edge.source,
          severity: 'error',
        });
      }
    }
  }

  // --------------------------------------------------------------------------
  // Reachability (BFS from start node)
  // --------------------------------------------------------------------------

  private validateReachability(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    errors: ValidationError[],
    warnings: ValidationWarning[],
  ): void {
    if (nodes.length === 0) return;

    const startNode = nodes.find(
      (n) => n.type === 'start' || n.type === 'GREETING',
    );
    if (!startNode) return;

    const adjacency = new Map<string, string[]>();
    for (const node of nodes) {
      adjacency.set(node.id, []);
    }
    for (const edge of edges) {
      const neighbors = adjacency.get(edge.source);
      if (neighbors) neighbors.push(edge.target);
    }

    // BFS
    const visited = new Set<string>();
    const queue = [startNode.id];
    visited.add(startNode.id);

    while (queue.length > 0) {
      const current = queue.shift()!;
      const neighbors = adjacency.get(current) || [];
      for (const neighbor of neighbors) {
        if (!visited.has(neighbor)) {
          visited.add(neighbor);
          queue.push(neighbor);
        }
      }
    }

    // Check unreachable nodes
    for (const node of nodes) {
      if (!visited.has(node.id) && node.id !== startNode.id) {
        warnings.push({
          code: 'UNREACHABLE_NODE',
          message: `Node "${node.data.label || node.id}" (${node.type}) is unreachable from the start node`,
          nodeId: node.id,
          severity: 'warning',
        });
      }
    }
  }

  // --------------------------------------------------------------------------
  // Cycle Detection for Infinite Loops
  // --------------------------------------------------------------------------

  private validateCyclesForInfiniteLoops(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    errors: ValidationError[],
    warnings: ValidationWarning[],
  ): void {
    if (nodes.length === 0) return;

    const adjacency = new Map<string, string[]>();
    for (const node of nodes) {
      adjacency.set(node.id, []);
    }
    for (const edge of edges) {
      const neighbors = adjacency.get(edge.source);
      if (neighbors) neighbors.push(edge.target);
    }

    // DFS cycle detection with coloring
    const WHITE = 0, GRAY = 1, BLACK = 2;
    const color = new Map<string, number>();
    for (const node of nodes) {
      color.set(node.id, WHITE);
    }

    const cycleNodes = new Set<string>();

    const dfs = (nodeId: string, path: string[]): boolean => {
      color.set(nodeId, GRAY);
      path.push(nodeId);

      const neighbors = adjacency.get(nodeId) || [];
      for (const neighbor of neighbors) {
        if (color.get(neighbor) === GRAY) {
          // Found a cycle - check if any node in the cycle can break it
          const cycleStart = path.indexOf(neighbor);
          const cyclePath = path.slice(cycleStart);

          // Check if cycle has a conditional or wait node (can potentially break)
          const hasBreakPoint = cyclePath.some((id) => {
            const node = nodes.find((n) => n.id === id);
            return (
              node &&
              (node.type === 'CONDITIONAL' ||
                node.type === 'WAIT' ||
                node.type === 'VERIFICATION' ||
                node.type === 'QUALIFICATION')
            );
          });

          // Check if the cycle edges have conditions
          const cycleEdges = edges.filter(
            (e) =>
              cyclePath.includes(e.source) &&
              cyclePath.includes(e.target),
          );
          const allEdgesConditional = cycleEdges.every(
            (e) => e.conditions && (e.conditions.expression || e.conditions.field),
          );

          if (!hasBreakPoint && !allEdgesConditional) {
            for (const id of cyclePath) cycleNodes.add(id);
          }
          return true;
        }
        if (color.get(neighbor) === WHITE) {
          dfs(neighbor, [...path]);
        }
      }

      color.set(nodeId, BLACK);
      return false;
    };

    for (const node of nodes) {
      if (color.get(node.id) === WHITE) {
        dfs(node.id, []);
      }
    }

    if (cycleNodes.size > 0) {
      warnings.push({
        code: 'POTENTIAL_INFINITE_LOOP',
        message: `Potential infinite loop detected involving nodes: ${[...cycleNodes].join(', ')}. Ensure conditional exits exist.`,
        severity: 'warning',
      });
    }
  }

  // --------------------------------------------------------------------------
  // Terminal Node Validation
  // --------------------------------------------------------------------------

  private validateTerminalNodes(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    errors: ValidationError[],
    warnings: ValidationWarning[],
  ): void {
    const endNodes = nodes.filter((n) => TERMINAL_NODE_TYPES.has(n.type));

    if (endNodes.length === 0) {
      errors.push({
        code: 'NO_END_NODE',
        message:
          'Workflow must have at least one terminal node (END_CALL, RETRY_LATER, or ESCALATE_HUMAN)',
        severity: 'error',
      });
    }

    // Terminal nodes should not have outgoing edges
    for (const endNode of endNodes) {
      const outgoing = edges.filter((e) => e.source === endNode.id);
      if (outgoing.length > 0) {
        warnings.push({
          code: 'TERMINAL_NODE_HAS_OUTGOING',
          message: `Terminal node "${endNode.data.label || endNode.id}" (${endNode.type}) has ${outgoing.length} outgoing edge(s) which will be ignored`,
          nodeId: endNode.id,
          severity: 'warning',
        });
      }
    }
  }

  // --------------------------------------------------------------------------
  // Node Configuration Validation
  // --------------------------------------------------------------------------

  private validateNodeConfigurations(
    nodes: WorkflowNode[],
    errors: ValidationError[],
    warnings: ValidationWarning[],
  ): void {
    for (const node of nodes) {
      if (node.type === 'start') continue;

      const requiredFields = NODE_CONFIG_REQUIREMENTS[node.type];
      if (!requiredFields) continue;

      const config = (node.data.config as Record<string, unknown>) || {};

      for (const field of requiredFields) {
        const value = config[field];
        if (value === undefined || value === null || value === '') {
          warnings.push({
            code: 'MISSING_NODE_CONFIG',
            message: `Node "${node.data.label || node.id}" (${node.type}) is missing required config field: ${field}`,
            nodeId: node.id,
            severity: 'warning',
          });
        }
      }

      // Type-specific validations
      if (node.type === 'OFFER_OPTIONS') {
        const options = config.options as unknown[] | undefined;
        if (options && options.length < 2) {
          warnings.push({
            code: 'INSUFFICIENT_OPTIONS',
            message: `Node "${node.data.label || node.id}" (OFFER_OPTIONS) should have at least 2 options`,
            nodeId: node.id,
            severity: 'warning',
          });
        }
      }

      if (node.type === 'API_CALL') {
        const url = config.url as string | undefined;
        if (url && !url.startsWith('http') && !url.includes('{{')) {
          warnings.push({
            code: 'INVALID_URL',
            message: `Node "${node.data.label || node.id}" (API_CALL) has a URL that does not start with http(s)`,
            nodeId: node.id,
            severity: 'warning',
          });
        }
      }

      if (node.type === 'SET_VARIABLE') {
        const assignments = config.assignments as unknown[] | undefined;
        if (!assignments || assignments.length === 0) {
          warnings.push({
            code: 'EMPTY_ASSIGNMENTS',
            message: `Node "${node.data.label || node.id}" (SET_VARIABLE) has no variable assignments`,
            nodeId: node.id,
            severity: 'warning',
          });
        }
      }

      if (node.type === 'QUALIFICATION') {
        const questions = config.questions as unknown[] | undefined;
        if (!questions || questions.length === 0) {
          warnings.push({
            code: 'NO_QUALIFICATION_QUESTIONS',
            message: `Node "${node.data.label || node.id}" (QUALIFICATION) has no questions configured`,
            nodeId: node.id,
            severity: 'warning',
          });
        }
      }
    }
  }

  // --------------------------------------------------------------------------
  // Edge Condition Validation
  // --------------------------------------------------------------------------

  private validateEdgeConditions(
    edges: WorkflowEdge[],
    warnings: ValidationWarning[],
  ): void {
    for (const edge of edges) {
      if (!edge.conditions) continue;

      const cond = edge.conditions;

      if (cond.expression) {
        // Basic syntax check for expressions
        const expr = cond.expression as string;
        try {
          // Try to parse as a function body to check syntax
          new Function(`"use strict"; return (${expr});`);
        } catch {
          warnings.push({
            code: 'INVALID_EDGE_EXPRESSION',
            message: `Edge ${edge.id} has an invalid expression: ${expr}`,
            edgeId: edge.id,
            severity: 'warning',
          });
        }
      }
    }
  }

  // --------------------------------------------------------------------------
  // Connectivity Warnings
  // --------------------------------------------------------------------------

  private validateNodeConnectivity(
    nodes: WorkflowNode[],
    edges: WorkflowEdge[],
    warnings: ValidationWarning[],
  ): void {
    const nodeIds = new Set(nodes.map((n) => n.id));

    for (const node of nodes) {
      if (node.type === 'start' || node.type === 'GREETING') continue;

      // Check for nodes that require outgoing edges
      if (NODES_REQUIRING_OUTGOING_EDGES.has(node.type)) {
        const outgoing = edges.filter((e) => e.source === node.id);
        if (outgoing.length === 0) {
          warnings.push({
            code: 'NO_OUTGOING_EDGES',
            message: `Node "${node.data.label || node.id}" (${node.type}) has no outgoing edges and will be a dead end`,
            nodeId: node.id,
            severity: 'warning',
          });
        }
      }

      // Check for nodes with no incoming edges (except start)
      const incoming = edges.filter((e) => e.target === node.id);
      if (incoming.length === 0) {
        warnings.push({
          code: 'NO_INCOMING_EDGES',
          message: `Node "${node.data.label || node.id}" (${node.type}) has no incoming edges`,
          nodeId: node.id,
          severity: 'warning',
        });
      }
    }

    // Check for CONDITIONAL nodes with only one outgoing edge
    const conditionalNodes = nodes.filter((n) => n.type === 'CONDITIONAL');
    for (const node of conditionalNodes) {
      const outgoing = edges.filter((e) => e.source === node.id);
      if (outgoing.length < 2) {
        warnings.push({
          code: 'CONDITIONAL_FEW_BRANCHES',
          message: `Conditional node "${node.data.label || node.id}" has fewer than 2 outgoing edges`,
          nodeId: node.id,
          severity: 'warning',
        });
      }
    }
  }
}
