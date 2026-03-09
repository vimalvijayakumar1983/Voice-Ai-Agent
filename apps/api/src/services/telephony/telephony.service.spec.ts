import { Test, TestingModule } from '@nestjs/testing';
import { ConfigService } from '@nestjs/config';
import { BadRequestException } from '@nestjs/common';
import { TelephonyService } from './telephony.service';

// ─── Provider Mocks ─────────────────────────────────────────────────────────

const mockTwilioProvider = {
  name: 'twilio',
  initiateCall: jest.fn(),
  endCall: jest.fn(),
  getCallStatus: jest.fn(),
  transferCall: jest.fn(),
  provisionNumber: jest.fn(),
  releaseNumber: jest.fn(),
  sendDtmf: jest.fn(),
};

const mockVonageProvider = {
  name: 'vonage',
  initiateCall: jest.fn(),
  endCall: jest.fn(),
  getCallStatus: jest.fn(),
  transferCall: jest.fn(),
  provisionNumber: jest.fn(),
  releaseNumber: jest.fn(),
  sendDtmf: jest.fn(),
};

const mockTelnyxProvider = {
  name: 'telnyx',
  initiateCall: jest.fn(),
  endCall: jest.fn(),
  getCallStatus: jest.fn(),
  transferCall: jest.fn(),
  provisionNumber: jest.fn(),
  releaseNumber: jest.fn(),
  sendDtmf: jest.fn(),
};

const mockConfigService = {
  get: jest.fn((key: string) => {
    const config: Record<string, string> = {
      TELEPHONY_DEFAULT_PROVIDER: 'twilio',
      TWILIO_ACCOUNT_SID: 'AC_test_sid',
      TWILIO_AUTH_TOKEN: 'test_auth_token',
      TWILIO_WEBHOOK_URL: 'https://api.voiceai.platform/webhooks/twilio',
      VONAGE_API_KEY: 'test_vonage_key',
      TELNYX_API_KEY: 'test_telnyx_key',
    };
    return config[key];
  }),
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('TelephonyService', () => {
  let service: TelephonyService;

  beforeEach(async () => {
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        TelephonyService,
        { provide: ConfigService, useValue: mockConfigService },
        { provide: 'TwilioProvider', useValue: mockTwilioProvider },
        { provide: 'VonageProvider', useValue: mockVonageProvider },
        { provide: 'TelnyxProvider', useValue: mockTelnyxProvider },
      ],
    }).compile();

    service = module.get<TelephonyService>(TelephonyService);

    jest.clearAllMocks();
  });

  describe('initiateCall', () => {
    const callParams = {
      to: '+14155551234',
      from: '+14155559876',
      callbackUrl: 'https://api.voiceai.platform/webhooks/call-status',
      metadata: { callId: 'call-1', tenantId: 'tenant-1' },
    };

    it('should initiate a call using the default provider (Twilio)', async () => {
      mockTwilioProvider.initiateCall.mockResolvedValue({
        externalId: 'CA_twilio_call_123',
        status: 'INITIATING',
        provider: 'twilio',
      });

      const result = await service.initiateCall(callParams);

      expect(result.provider).toBe('twilio');
      expect(result.externalId).toBe('CA_twilio_call_123');
      expect(mockTwilioProvider.initiateCall).toHaveBeenCalledWith(callParams);
    });

    it('should initiate a call with a specific provider (Vonage)', async () => {
      mockVonageProvider.initiateCall.mockResolvedValue({
        externalId: 'vonage_call_456',
        status: 'INITIATING',
        provider: 'vonage',
      });

      const result = await service.initiateCall({ ...callParams, provider: 'vonage' });

      expect(result.provider).toBe('vonage');
      expect(mockVonageProvider.initiateCall).toHaveBeenCalled();
      expect(mockTwilioProvider.initiateCall).not.toHaveBeenCalled();
    });

    it('should initiate a call with Telnyx provider', async () => {
      mockTelnyxProvider.initiateCall.mockResolvedValue({
        externalId: 'telnyx_call_789',
        status: 'INITIATING',
        provider: 'telnyx',
      });

      const result = await service.initiateCall({ ...callParams, provider: 'telnyx' });

      expect(result.provider).toBe('telnyx');
      expect(mockTelnyxProvider.initiateCall).toHaveBeenCalled();
    });

    it('should throw BadRequestException for unsupported provider', async () => {
      await expect(
        service.initiateCall({ ...callParams, provider: 'unsupported' as any }),
      ).rejects.toThrow(BadRequestException);
    });
  });

  describe('endCall', () => {
    it('should end a call through the correct provider', async () => {
      mockTwilioProvider.endCall.mockResolvedValue({ status: 'COMPLETED' });

      const result = await service.endCall('CA_twilio_123', 'twilio');

      expect(result.status).toBe('COMPLETED');
      expect(mockTwilioProvider.endCall).toHaveBeenCalledWith('CA_twilio_123');
    });

    it('should end a Vonage call through Vonage provider', async () => {
      mockVonageProvider.endCall.mockResolvedValue({ status: 'COMPLETED' });

      const result = await service.endCall('vonage_456', 'vonage');

      expect(result.status).toBe('COMPLETED');
      expect(mockVonageProvider.endCall).toHaveBeenCalledWith('vonage_456');
    });
  });

  describe('getCallStatus', () => {
    it('should retrieve call status from the correct provider', async () => {
      mockTwilioProvider.getCallStatus.mockResolvedValue({
        externalId: 'CA_twilio_123',
        status: 'IN_PROGRESS',
        duration: 45,
      });

      const result = await service.getCallStatus('CA_twilio_123', 'twilio');

      expect(result.status).toBe('IN_PROGRESS');
      expect(result.duration).toBe(45);
    });
  });

  describe('transferCall', () => {
    it('should transfer an active call to a new number', async () => {
      mockTwilioProvider.transferCall.mockResolvedValue({
        status: 'TRANSFERRED',
        transferredTo: '+14155550000',
      });

      const result = await service.transferCall('CA_twilio_123', '+14155550000', 'twilio');

      expect(result.status).toBe('TRANSFERRED');
      expect(result.transferredTo).toBe('+14155550000');
    });
  });

  describe('provisionNumber', () => {
    it('should provision a new phone number via Twilio', async () => {
      mockTwilioProvider.provisionNumber.mockResolvedValue({
        number: '+14155552222',
        provider: 'twilio',
        capabilities: ['VOICE', 'SMS'],
      });

      const result = await service.provisionNumber({ areaCode: '415', provider: 'twilio' });

      expect(result.number).toBe('+14155552222');
      expect(result.capabilities).toContain('VOICE');
    });
  });

  describe('releaseNumber', () => {
    it('should release a phone number back to the provider', async () => {
      mockTwilioProvider.releaseNumber.mockResolvedValue({ released: true });

      const result = await service.releaseNumber('+14155552222', 'twilio');

      expect(result.released).toBe(true);
    });
  });
});
