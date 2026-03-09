import { Test, TestingModule } from '@nestjs/testing';
import { NotFoundException, ForbiddenException } from '@nestjs/common';
import { AgentsService } from './agents.service';
import { PrismaService } from '../../common/services/prisma.service';

// ─── Mocks ──────────────────────────────────────────────────────────────────

const mockPrismaService = {
  agent: {
    findMany: jest.fn(),
    findUnique: jest.fn(),
    findFirst: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
    delete: jest.fn(),
    count: jest.fn(),
  },
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('AgentsService', () => {
  let service: AgentsService;

  const tenantId = 'tenant-healthcare-1';
  const otherTenantId = 'tenant-sales-2';

  const mockAgent = {
    id: 'agent-1',
    name: 'Appointment Booking Agent',
    description: 'Handles scheduling of patient appointments.',
    tenantId,
    voiceId: 'alloy',
    language: 'en-US',
    isActive: true,
    systemPrompt: 'You are a medical receptionist...',
    llmProvider: 'openai',
    llmModel: 'gpt-4o',
    temperature: 0.7,
    maxTokens: 2048,
    createdById: 'user-1',
    createdAt: new Date('2026-01-15'),
    updatedAt: new Date('2026-01-15'),
  };

  beforeEach(async () => {
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        AgentsService,
        { provide: PrismaService, useValue: mockPrismaService },
      ],
    }).compile();

    service = module.get<AgentsService>(AgentsService);

    jest.clearAllMocks();
  });

  describe('findAll', () => {
    it('should return agents only for the specified tenant', async () => {
      const agents = [mockAgent, { ...mockAgent, id: 'agent-2', name: 'Follow-up Agent' }];
      mockPrismaService.agent.findMany.mockResolvedValue(agents);
      mockPrismaService.agent.count.mockResolvedValue(2);

      const result = await service.findAll(tenantId, { page: 1, limit: 10 });

      expect(mockPrismaService.agent.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          where: expect.objectContaining({ tenantId }),
        }),
      );
      expect(result.data).toHaveLength(2);
      expect(result.data.every((a: any) => a.tenantId === tenantId)).toBe(true);
    });

    it('should support pagination parameters', async () => {
      mockPrismaService.agent.findMany.mockResolvedValue([]);
      mockPrismaService.agent.count.mockResolvedValue(0);

      await service.findAll(tenantId, { page: 2, limit: 5 });

      expect(mockPrismaService.agent.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          skip: 5,
          take: 5,
        }),
      );
    });

    it('should filter agents by active status when specified', async () => {
      mockPrismaService.agent.findMany.mockResolvedValue([mockAgent]);
      mockPrismaService.agent.count.mockResolvedValue(1);

      await service.findAll(tenantId, { page: 1, limit: 10, isActive: true });

      expect(mockPrismaService.agent.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          where: expect.objectContaining({ tenantId, isActive: true }),
        }),
      );
    });
  });

  describe('findOne', () => {
    it('should return an agent by id within the tenant', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(mockAgent);

      const result = await service.findOne('agent-1', tenantId);

      expect(result).toEqual(mockAgent);
      expect(mockPrismaService.agent.findFirst).toHaveBeenCalledWith(
        expect.objectContaining({
          where: { id: 'agent-1', tenantId },
        }),
      );
    });

    it('should throw NotFoundException if agent does not exist', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(null);

      await expect(service.findOne('nonexistent', tenantId)).rejects.toThrow(NotFoundException);
    });

    it('should not return an agent belonging to a different tenant', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(null);

      await expect(service.findOne('agent-1', otherTenantId)).rejects.toThrow(NotFoundException);
    });
  });

  describe('create', () => {
    const createDto = {
      name: 'New Lab Notifier Agent',
      description: 'Notifies patients about lab results.',
      voiceId: 'shimmer',
      language: 'en-US',
      systemPrompt: 'You are a lab results coordinator...',
      llmProvider: 'openai',
      llmModel: 'gpt-4o',
    };

    it('should create an agent scoped to the tenant', async () => {
      const createdAgent = { id: 'agent-new', ...createDto, tenantId, createdById: 'user-1' };
      mockPrismaService.agent.create.mockResolvedValue(createdAgent);

      const result = await service.create(createDto, tenantId, 'user-1');

      expect(result.tenantId).toBe(tenantId);
      expect(mockPrismaService.agent.create).toHaveBeenCalledWith(
        expect.objectContaining({
          data: expect.objectContaining({
            name: createDto.name,
            tenantId,
            createdById: 'user-1',
          }),
        }),
      );
    });

    it('should set default values for optional fields', async () => {
      mockPrismaService.agent.create.mockResolvedValue({
        id: 'agent-new',
        ...createDto,
        tenantId,
        isActive: true,
        temperature: 0.7,
        maxTokens: 2048,
      });

      const result = await service.create(createDto, tenantId, 'user-1');

      expect(result.isActive).toBe(true);
      expect(result.temperature).toBe(0.7);
    });
  });

  describe('update', () => {
    it('should update an agent within the same tenant', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(mockAgent);
      const updated = { ...mockAgent, name: 'Updated Agent Name' };
      mockPrismaService.agent.update.mockResolvedValue(updated);

      const result = await service.update('agent-1', { name: 'Updated Agent Name' }, tenantId);

      expect(result.name).toBe('Updated Agent Name');
    });

    it('should throw NotFoundException when updating agent from another tenant', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(null);

      await expect(
        service.update('agent-1', { name: 'Hacked' }, otherTenantId),
      ).rejects.toThrow(NotFoundException);
    });
  });

  describe('delete', () => {
    it('should soft-delete an agent by setting isActive to false', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(mockAgent);
      mockPrismaService.agent.update.mockResolvedValue({ ...mockAgent, isActive: false });

      await service.remove('agent-1', tenantId);

      expect(mockPrismaService.agent.update).toHaveBeenCalledWith(
        expect.objectContaining({
          where: { id: 'agent-1' },
          data: expect.objectContaining({ isActive: false }),
        }),
      );
    });

    it('should throw NotFoundException when deleting agent from wrong tenant', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(null);

      await expect(service.remove('agent-1', otherTenantId)).rejects.toThrow(NotFoundException);
    });
  });
});
