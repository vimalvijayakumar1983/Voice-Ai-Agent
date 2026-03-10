import {
  Injectable,
  NotFoundException,
  BadRequestException,
  Logger,
} from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { CreateWorkflowDto, UpdateWorkflowDto } from './dto/create-workflow.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

@Injectable()
export class WorkflowsService {
  private readonly logger = new Logger(WorkflowsService.name);

  constructor(private readonly prisma: PrismaService) {}

  async findAll(tenantId: string, query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;
    const where = this.prisma.tenantWhere(tenantId);

    const [workflows, total] = await Promise.all([
      this.prisma.workflow.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
      }),
      this.prisma.workflow.count({ where }),
    ]);

    return new PaginatedResponse(workflows, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const workflow = await this.prisma.workflow.findFirst({
      where: { id, tenantId },
    });
    if (!workflow) throw new NotFoundException(`Workflow ${id} not found`);
    return workflow;
  }

  async create(tenantId: string, userId: string, dto: CreateWorkflowDto): Promise<any> {
    this.validateWorkflow(dto);

    return this.prisma.workflow.create({
      data: {
        name: dto.name,
        description: dto.description,
        nodes: JSON.parse(JSON.stringify(dto.nodes)),
        edges: JSON.parse(JSON.stringify(dto.edges)),
        agentId: dto.agentId,
        tenantId,
        createdById: userId,
        version: 1,
        isActive: dto.isActive ?? true,
        metadata: (dto.metadata || {}) as any,
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateWorkflowDto): Promise<any> {
    await this.findById(tenantId, id);

    if (dto.nodes && dto.edges) {
      this.validateWorkflow(dto as CreateWorkflowDto);
    }

    const updateData: Record<string, unknown> = {};
    if (dto.name !== undefined) updateData.name = dto.name;
    if (dto.description !== undefined) updateData.description = dto.description;
    if (dto.nodes) updateData.nodes = JSON.parse(JSON.stringify(dto.nodes));
    if (dto.edges) updateData.edges = JSON.parse(JSON.stringify(dto.edges));
    if (dto.agentId !== undefined) updateData.agentId = dto.agentId;
    if (dto.isActive !== undefined) updateData.isActive = dto.isActive;
    if (dto.metadata) updateData.metadata = dto.metadata;

    return this.prisma.workflow.update({
      where: { id },
      data: updateData,
    });
  }

  async delete(tenantId: string, id: string): Promise<void> {
    await this.findById(tenantId, id);
    await this.prisma.workflow.update({
      where: { id },
      data: { isActive: false },
    });
  }

  async createVersion(tenantId: string, id: string): Promise<any> {
    const workflow = await this.findById(tenantId, id);
    const newVersion = (workflow.version || 1) + 1;

    const versionRecord = await this.prisma.workflowVersion.create({
      data: {
        workflow: { connect: { id } },
        version: workflow.version,
        definition: JSON.parse(JSON.stringify(workflow)),
        tenantId,
      },
    });

    await this.prisma.workflow.update({
      where: { id },
      data: { version: newVersion },
    });

    return versionRecord;
  }

  async validate(tenantId: string, id: string): Promise<{ valid: boolean; errors: string[] }> {
    const workflow = await this.findById(tenantId, id);
    const errors: string[] = [];
    const nodes = workflow.nodes as any[];
    const edges = workflow.edges as any[];

    if (!nodes || nodes.length === 0) {
      errors.push('Workflow must have at least one node');
    }

    const startNodes = nodes.filter((n) => n.type === 'start');
    if (startNodes.length !== 1) {
      errors.push('Workflow must have exactly one start node');
    }

    const nodeIds = new Set(nodes.map((n) => n.id));
    for (const edge of edges) {
      if (!nodeIds.has(edge.source)) {
        errors.push(`Edge references non-existent source node: ${edge.source}`);
      }
      if (!nodeIds.has(edge.target)) {
        errors.push(`Edge references non-existent target node: ${edge.target}`);
      }
    }

    return { valid: errors.length === 0, errors };
  }

  async clone(tenantId: string, id: string, newName: string): Promise<any> {
    const source = await this.findById(tenantId, id);
    const { id: _id, createdAt, updatedAt, ...sourceData } = source;

    return this.prisma.workflow.create({
      data: {
        ...sourceData,
        name: newName || `${source.name} (Copy)`,
        version: 1,
        tenantId,
      },
    });
  }

  private validateWorkflow(dto: CreateWorkflowDto): void {
    if (!dto.nodes || dto.nodes.length === 0) {
      throw new BadRequestException('Workflow must have at least one node');
    }

    const nodeIds = new Set(dto.nodes.map((n) => n.id));
    if (nodeIds.size !== dto.nodes.length) {
      throw new BadRequestException('Duplicate node IDs detected');
    }

    for (const edge of dto.edges || []) {
      if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
        throw new BadRequestException(`Edge references non-existent node`);
      }
    }
  }
}
