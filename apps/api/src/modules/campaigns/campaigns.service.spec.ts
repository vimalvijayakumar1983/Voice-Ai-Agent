import { Test, TestingModule } from '@nestjs/testing';
import { NotFoundException, BadRequestException } from '@nestjs/common';
import { CampaignsService } from './campaigns.service';
import { PrismaService } from '../../common/services/prisma.service';

// ─── Mocks ──────────────────────────────────────────────────────────────────

const mockPrismaService = {
  campaign: {
    findMany: jest.fn(),
    findFirst: jest.fn(),
    findUnique: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
    delete: jest.fn(),
    count: jest.fn(),
  },
  contact: {
    findMany: jest.fn(),
    count: jest.fn(),
  },
  call: {
    count: jest.fn(),
    findMany: jest.fn(),
  },
  $transaction: jest.fn((fn: (prisma: any) => Promise<any>) => fn(mockPrismaService)),
};

const mockQueue = {
  add: jest.fn(),
  getJob: jest.fn(),
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('CampaignsService', () => {
  let service: CampaignsService;

  const tenantId = 'tenant-collections-1';

  const mockCampaign = {
    id: 'campaign-1',
    name: 'March Overdue Accounts',
    description: 'Outreach to all accounts 30+ days overdue.',
    tenantId,
    status: 'DRAFT',
    agentId: 'agent-1',
    phoneNumberId: 'phone-1',
    contactIds: ['contact-1', 'contact-2', 'contact-3'],
    scheduledAt: null,
    startedAt: null,
    completedAt: null,
    settings: {
      maxConcurrentCalls: 5,
      retryAttempts: 2,
      retryDelayMinutes: 60,
    },
    createdById: 'user-1',
    createdAt: new Date('2026-03-01'),
    updatedAt: new Date('2026-03-01'),
  };

  beforeEach(async () => {
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        CampaignsService,
        { provide: PrismaService, useValue: mockPrismaService },
        { provide: 'BullQueue_campaigns', useValue: mockQueue },
      ],
    }).compile();

    service = module.get<CampaignsService>(CampaignsService);

    jest.clearAllMocks();
  });

  describe('create', () => {
    const createDto = {
      name: 'New Campaign',
      description: 'Test campaign for collections.',
      agentId: 'agent-1',
      phoneNumberId: 'phone-1',
      contactIds: ['contact-1', 'contact-2'],
      settings: { maxConcurrentCalls: 3 },
    };

    it('should create a campaign in DRAFT status', async () => {
      const created = { id: 'campaign-new', ...createDto, tenantId, status: 'DRAFT' };
      mockPrismaService.campaign.create.mockResolvedValue(created);

      const result = await service.create(createDto, tenantId, 'user-1');

      expect(result.status).toBe('DRAFT');
      expect(mockPrismaService.campaign.create).toHaveBeenCalledWith(
        expect.objectContaining({
          data: expect.objectContaining({
            name: 'New Campaign',
            tenantId,
            status: 'DRAFT',
          }),
        }),
      );
    });

    it('should associate contacts with the campaign', async () => {
      const created = { id: 'campaign-new', ...createDto, tenantId, status: 'DRAFT' };
      mockPrismaService.campaign.create.mockResolvedValue(created);

      const result = await service.create(createDto, tenantId, 'user-1');

      expect(result.contactIds).toEqual(['contact-1', 'contact-2']);
    });
  });

  describe('lifecycle transitions', () => {
    it('should transition campaign from DRAFT to SCHEDULED', async () => {
      mockPrismaService.campaign.findFirst.mockResolvedValue(mockCampaign);
      const scheduled = { ...mockCampaign, status: 'SCHEDULED', scheduledAt: new Date() };
      mockPrismaService.campaign.update.mockResolvedValue(scheduled);

      const result = await service.schedule('campaign-1', tenantId, new Date());

      expect(result.status).toBe('SCHEDULED');
      expect(result.scheduledAt).toBeDefined();
    });

    it('should transition campaign from SCHEDULED to IN_PROGRESS when started', async () => {
      const scheduledCampaign = { ...mockCampaign, status: 'SCHEDULED', scheduledAt: new Date() };
      mockPrismaService.campaign.findFirst.mockResolvedValue(scheduledCampaign);
      const started = { ...scheduledCampaign, status: 'IN_PROGRESS', startedAt: new Date() };
      mockPrismaService.campaign.update.mockResolvedValue(started);

      const result = await service.start('campaign-1', tenantId);

      expect(result.status).toBe('IN_PROGRESS');
      expect(result.startedAt).toBeDefined();
    });

    it('should transition campaign from IN_PROGRESS to PAUSED', async () => {
      const activeCampaign = { ...mockCampaign, status: 'IN_PROGRESS', startedAt: new Date() };
      mockPrismaService.campaign.findFirst.mockResolvedValue(activeCampaign);
      const paused = { ...activeCampaign, status: 'PAUSED' };
      mockPrismaService.campaign.update.mockResolvedValue(paused);

      const result = await service.pause('campaign-1', tenantId);

      expect(result.status).toBe('PAUSED');
    });

    it('should transition campaign from IN_PROGRESS to COMPLETED', async () => {
      const activeCampaign = { ...mockCampaign, status: 'IN_PROGRESS', startedAt: new Date() };
      mockPrismaService.campaign.findFirst.mockResolvedValue(activeCampaign);
      const completed = { ...activeCampaign, status: 'COMPLETED', completedAt: new Date() };
      mockPrismaService.campaign.update.mockResolvedValue(completed);

      const result = await service.complete('campaign-1', tenantId);

      expect(result.status).toBe('COMPLETED');
      expect(result.completedAt).toBeDefined();
    });

    it('should reject invalid state transition from COMPLETED to IN_PROGRESS', async () => {
      const completedCampaign = { ...mockCampaign, status: 'COMPLETED', completedAt: new Date() };
      mockPrismaService.campaign.findFirst.mockResolvedValue(completedCampaign);

      await expect(service.start('campaign-1', tenantId)).rejects.toThrow(BadRequestException);
    });

    it('should reject starting a DRAFT campaign without scheduling first', async () => {
      mockPrismaService.campaign.findFirst.mockResolvedValue(mockCampaign);

      await expect(service.start('campaign-1', tenantId)).rejects.toThrow(BadRequestException);
    });
  });

  describe('getStats', () => {
    it('should return campaign statistics with call counts', async () => {
      const activeCampaign = { ...mockCampaign, status: 'IN_PROGRESS', contactIds: ['c1', 'c2', 'c3', 'c4', 'c5'] };
      mockPrismaService.campaign.findFirst.mockResolvedValue(activeCampaign);
      mockPrismaService.call.count
        .mockResolvedValueOnce(5) // total
        .mockResolvedValueOnce(3) // completed
        .mockResolvedValueOnce(1) // no answer
        .mockResolvedValueOnce(1); // failed

      const stats = await service.getStats('campaign-1', tenantId);

      expect(stats).toEqual(
        expect.objectContaining({
          totalContacts: 5,
          callsMade: 5,
          callsCompleted: 3,
        }),
      );
    });
  });

  describe('tenant isolation', () => {
    it('should not allow access to campaigns from another tenant', async () => {
      mockPrismaService.campaign.findFirst.mockResolvedValue(null);

      await expect(
        service.findOne('campaign-1', 'other-tenant-id'),
      ).rejects.toThrow(NotFoundException);
    });
  });
});
