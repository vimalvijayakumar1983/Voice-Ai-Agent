import { Test, TestingModule } from '@nestjs/testing';
import { NotFoundException, BadRequestException } from '@nestjs/common';
import { CallsService } from './calls.service';
import { PrismaService } from '../../common/services/prisma.service';

// ─── Mocks ──────────────────────────────────────────────────────────────────

const mockPrismaService = {
  call: {
    findMany: jest.fn(),
    findFirst: jest.fn(),
    findUnique: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
    count: jest.fn(),
    aggregate: jest.fn(),
  },
  agent: {
    findFirst: jest.fn(),
  },
  contact: {
    findFirst: jest.fn(),
  },
  phoneNumber: {
    findFirst: jest.fn(),
  },
};

const mockEventEmitter = {
  emit: jest.fn(),
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('CallsService', () => {
  let service: CallsService;

  const tenantId = 'tenant-healthcare-1';

  const mockCall = {
    id: 'call-1',
    tenantId,
    agentId: 'agent-1',
    contactId: 'contact-1',
    phoneNumberId: 'phone-1',
    campaignId: 'campaign-1',
    direction: 'OUTBOUND',
    status: 'COMPLETED',
    duration: 185,
    startedAt: new Date('2026-03-05T10:00:00Z'),
    endedAt: new Date('2026-03-05T10:03:05Z'),
    transcript: [
      { role: 'agent', text: 'Hello, this is MedCare Health Systems.', timestamp: 0, sentiment: 'neutral' },
      { role: 'contact', text: 'Hi, yes I received your message.', timestamp: 3.5, sentiment: 'neutral' },
      { role: 'agent', text: 'Great, I am calling about your upcoming appointment.', timestamp: 6.0, sentiment: 'positive' },
    ],
    summary: 'Appointment confirmed for Thursday at 10 AM.',
    outcome: 'APPOINTMENT_BOOKED',
    sentiment: 'POSITIVE',
    recordingUrl: 'https://storage.voiceai.platform/recordings/tenant-1/call-1.wav',
    cost: 0.045,
    metadata: { provider: 'twilio', callSid: 'CA123abc' },
    createdAt: new Date('2026-03-05T10:00:00Z'),
  };

  beforeEach(async () => {
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        CallsService,
        { provide: PrismaService, useValue: mockPrismaService },
        { provide: 'EventEmitter', useValue: mockEventEmitter },
      ],
    }).compile();

    service = module.get<CallsService>(CallsService);

    jest.clearAllMocks();
  });

  describe('findAll', () => {
    it('should return paginated calls for a tenant', async () => {
      const calls = [mockCall, { ...mockCall, id: 'call-2' }];
      mockPrismaService.call.findMany.mockResolvedValue(calls);
      mockPrismaService.call.count.mockResolvedValue(2);

      const result = await service.findAll(tenantId, { page: 1, limit: 10 });

      expect(result.data).toHaveLength(2);
      expect(mockPrismaService.call.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          where: expect.objectContaining({ tenantId }),
        }),
      );
    });

    it('should filter calls by status', async () => {
      mockPrismaService.call.findMany.mockResolvedValue([mockCall]);
      mockPrismaService.call.count.mockResolvedValue(1);

      await service.findAll(tenantId, { page: 1, limit: 10, status: 'COMPLETED' });

      expect(mockPrismaService.call.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          where: expect.objectContaining({ tenantId, status: 'COMPLETED' }),
        }),
      );
    });

    it('should filter calls by date range', async () => {
      mockPrismaService.call.findMany.mockResolvedValue([]);
      mockPrismaService.call.count.mockResolvedValue(0);

      const startDate = new Date('2026-03-01');
      const endDate = new Date('2026-03-07');

      await service.findAll(tenantId, { page: 1, limit: 10, startDate, endDate });

      expect(mockPrismaService.call.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          where: expect.objectContaining({
            tenantId,
            startedAt: { gte: startDate, lte: endDate },
          }),
        }),
      );
    });

    it('should filter calls by agent', async () => {
      mockPrismaService.call.findMany.mockResolvedValue([mockCall]);
      mockPrismaService.call.count.mockResolvedValue(1);

      await service.findAll(tenantId, { page: 1, limit: 10, agentId: 'agent-1' });

      expect(mockPrismaService.call.findMany).toHaveBeenCalledWith(
        expect.objectContaining({
          where: expect.objectContaining({ tenantId, agentId: 'agent-1' }),
        }),
      );
    });
  });

  describe('findOne', () => {
    it('should return a call with transcript', async () => {
      mockPrismaService.call.findFirst.mockResolvedValue(mockCall);

      const result = await service.findOne('call-1', tenantId);

      expect(result).toEqual(mockCall);
      expect(result.transcript).toHaveLength(3);
    });

    it('should throw NotFoundException for non-existent call', async () => {
      mockPrismaService.call.findFirst.mockResolvedValue(null);

      await expect(service.findOne('nonexistent', tenantId)).rejects.toThrow(NotFoundException);
    });
  });

  describe('initiate', () => {
    const initiateDto = {
      agentId: 'agent-1',
      contactId: 'contact-1',
      phoneNumberId: 'phone-1',
    };

    it('should create a new outbound call record', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue({ id: 'agent-1', tenantId, isActive: true });
      mockPrismaService.contact.findFirst.mockResolvedValue({ id: 'contact-1', tenantId, phone: '+14155551234' });
      mockPrismaService.phoneNumber.findFirst.mockResolvedValue({ id: 'phone-1', tenantId, isActive: true });

      const newCall = {
        id: 'call-new',
        tenantId,
        ...initiateDto,
        direction: 'OUTBOUND',
        status: 'INITIATING',
        startedAt: new Date(),
      };
      mockPrismaService.call.create.mockResolvedValue(newCall);

      const result = await service.initiate(initiateDto, tenantId);

      expect(result.status).toBe('INITIATING');
      expect(result.direction).toBe('OUTBOUND');
    });

    it('should throw NotFoundException if agent does not belong to tenant', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue(null);

      await expect(service.initiate(initiateDto, tenantId)).rejects.toThrow(NotFoundException);
    });

    it('should throw BadRequestException if agent is inactive', async () => {
      mockPrismaService.agent.findFirst.mockResolvedValue({ id: 'agent-1', tenantId, isActive: false });

      await expect(service.initiate(initiateDto, tenantId)).rejects.toThrow(BadRequestException);
    });
  });

  describe('updateStatus', () => {
    it('should update call status and emit event', async () => {
      mockPrismaService.call.findFirst.mockResolvedValue({ ...mockCall, status: 'RINGING' });
      mockPrismaService.call.update.mockResolvedValue({ ...mockCall, status: 'IN_PROGRESS' });

      const result = await service.updateStatus('call-1', 'IN_PROGRESS', tenantId);

      expect(result.status).toBe('IN_PROGRESS');
      expect(mockEventEmitter.emit).toHaveBeenCalledWith(
        'call.status.changed',
        expect.objectContaining({ callId: 'call-1', status: 'IN_PROGRESS' }),
      );
    });
  });

  describe('getAnalytics', () => {
    it('should return aggregated call metrics for a tenant', async () => {
      mockPrismaService.call.count.mockResolvedValue(50);
      mockPrismaService.call.aggregate.mockResolvedValue({
        _avg: { duration: 180 },
        _sum: { cost: 12.5 },
      });

      const analytics = await service.getAnalytics(tenantId, {
        startDate: new Date('2026-03-01'),
        endDate: new Date('2026-03-09'),
      });

      expect(analytics).toEqual(
        expect.objectContaining({
          totalCalls: 50,
          avgDuration: 180,
          totalCost: 12.5,
        }),
      );
    });
  });
});
