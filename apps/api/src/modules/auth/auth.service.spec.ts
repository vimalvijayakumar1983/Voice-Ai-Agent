import { Test, TestingModule } from '@nestjs/testing';
import { JwtService } from '@nestjs/jwt';
import { ConfigService } from '@nestjs/config';
import { UnauthorizedException, ConflictException } from '@nestjs/common';
import { AuthService } from './auth.service';
import { PrismaService } from '../../common/services/prisma.service';
import * as bcrypt from 'bcryptjs';

// ─── Mocks ──────────────────────────────────────────────────────────────────

jest.mock('bcryptjs');

const mockPrismaService = {
  user: {
    findUnique: jest.fn(),
    findFirst: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
  },
  tenant: {
    create: jest.fn(),
  },
  $transaction: jest.fn((fn: (prisma: any) => Promise<any>) => fn(mockPrismaService)),
};

const mockJwtService = {
  sign: jest.fn(),
  signAsync: jest.fn(),
  verify: jest.fn(),
  verifyAsync: jest.fn(),
};

const mockConfigService = {
  get: jest.fn((key: string) => {
    const config: Record<string, string> = {
      JWT_SECRET: 'test-secret-key-for-testing',
      JWT_EXPIRATION: '1h',
      JWT_REFRESH_EXPIRATION: '7d',
    };
    return config[key];
  }),
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('AuthService', () => {
  let service: AuthService;

  beforeEach(async () => {
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        AuthService,
        { provide: PrismaService, useValue: mockPrismaService },
        { provide: JwtService, useValue: mockJwtService },
        { provide: ConfigService, useValue: mockConfigService },
      ],
    }).compile();

    service = module.get<AuthService>(AuthService);

    jest.clearAllMocks();
  });

  describe('register', () => {
    const registerDto = {
      email: 'newuser@example.com',
      password: 'SecurePass123!',
      firstName: 'Jane',
      lastName: 'Doe',
      tenantName: 'Test Corp',
    };

    it('should register a new user and create a tenant', async () => {
      mockPrismaService.user.findFirst.mockResolvedValue(null);
      (bcrypt.hash as jest.Mock).mockResolvedValue('hashed-password');

      const mockTenant = { id: 'tenant-1', name: 'Test Corp', slug: 'test-corp' };
      const mockUser = {
        id: 'user-1',
        email: registerDto.email,
        firstName: 'Jane',
        lastName: 'Doe',
        role: 'TENANT_OWNER',
        tenantId: 'tenant-1',
      };

      mockPrismaService.tenant.create.mockResolvedValue(mockTenant);
      mockPrismaService.user.create.mockResolvedValue(mockUser);
      mockJwtService.signAsync.mockResolvedValue('mock-jwt-token');

      const result = await service.register(registerDto);

      expect(result).toBeDefined();
      expect(mockPrismaService.user.findFirst).toHaveBeenCalledWith(
        expect.objectContaining({ where: { email: registerDto.email } }),
      );
      expect(bcrypt.hash).toHaveBeenCalledWith(registerDto.password, 10);
    });

    it('should throw ConflictException if email already exists', async () => {
      mockPrismaService.user.findFirst.mockResolvedValue({
        id: 'existing-user',
        email: registerDto.email,
      });

      await expect(service.register(registerDto)).rejects.toThrow(ConflictException);
    });

    it('should hash the password with bcrypt rounds of 10', async () => {
      mockPrismaService.user.findFirst.mockResolvedValue(null);
      (bcrypt.hash as jest.Mock).mockResolvedValue('hashed-password');
      mockPrismaService.tenant.create.mockResolvedValue({ id: 'tenant-1' });
      mockPrismaService.user.create.mockResolvedValue({ id: 'user-1', email: registerDto.email, tenantId: 'tenant-1', role: 'TENANT_OWNER' });
      mockJwtService.signAsync.mockResolvedValue('token');

      await service.register(registerDto);

      expect(bcrypt.hash).toHaveBeenCalledWith('SecurePass123!', 10);
    });
  });

  describe('login', () => {
    const loginDto = {
      email: 'user@example.com',
      password: 'SecurePass123!',
    };

    const mockUser = {
      id: 'user-1',
      email: 'user@example.com',
      passwordHash: 'hashed-password',
      firstName: 'John',
      lastName: 'Doe',
      role: 'TENANT_OWNER',
      tenantId: 'tenant-1',
      isActive: true,
      emailVerified: true,
    };

    it('should return access and refresh tokens for valid credentials', async () => {
      mockPrismaService.user.findUnique.mockResolvedValue(mockUser);
      (bcrypt.compare as jest.Mock).mockResolvedValue(true);
      mockJwtService.signAsync
        .mockResolvedValueOnce('access-token')
        .mockResolvedValueOnce('refresh-token');

      const result = await service.login(loginDto);

      expect(result).toEqual(
        expect.objectContaining({
          accessToken: 'access-token',
          refreshToken: 'refresh-token',
        }),
      );
      expect(mockPrismaService.user.update).toHaveBeenCalledWith(
        expect.objectContaining({
          where: { id: 'user-1' },
          data: expect.objectContaining({ lastLoginAt: expect.any(Date) }),
        }),
      );
    });

    it('should throw UnauthorizedException for non-existent email', async () => {
      mockPrismaService.user.findUnique.mockResolvedValue(null);

      await expect(service.login(loginDto)).rejects.toThrow(UnauthorizedException);
    });

    it('should throw UnauthorizedException for incorrect password', async () => {
      mockPrismaService.user.findUnique.mockResolvedValue(mockUser);
      (bcrypt.compare as jest.Mock).mockResolvedValue(false);

      await expect(service.login(loginDto)).rejects.toThrow(UnauthorizedException);
    });

    it('should throw UnauthorizedException for inactive user', async () => {
      mockPrismaService.user.findUnique.mockResolvedValue({ ...mockUser, isActive: false });
      (bcrypt.compare as jest.Mock).mockResolvedValue(true);

      await expect(service.login(loginDto)).rejects.toThrow(UnauthorizedException);
    });
  });

  describe('token generation', () => {
    it('should generate JWT with correct payload fields', async () => {
      const user = {
        id: 'user-1',
        email: 'user@example.com',
        role: 'TENANT_OWNER',
        tenantId: 'tenant-1',
      };

      mockJwtService.signAsync.mockResolvedValue('signed-token');

      await service.generateTokens(user as any);

      expect(mockJwtService.signAsync).toHaveBeenCalledWith(
        expect.objectContaining({
          sub: 'user-1',
          email: 'user@example.com',
          role: 'TENANT_OWNER',
          tenantId: 'tenant-1',
        }),
        expect.any(Object),
      );
    });

    it('should validate a refresh token and return new tokens', async () => {
      const payload = { sub: 'user-1', email: 'user@example.com', tenantId: 'tenant-1', role: 'TENANT_OWNER' };
      mockJwtService.verifyAsync.mockResolvedValue(payload);
      mockPrismaService.user.findUnique.mockResolvedValue({
        id: 'user-1',
        email: 'user@example.com',
        role: 'TENANT_OWNER',
        tenantId: 'tenant-1',
        isActive: true,
      });
      mockJwtService.signAsync
        .mockResolvedValueOnce('new-access-token')
        .mockResolvedValueOnce('new-refresh-token');

      const result = await service.refreshTokens('valid-refresh-token');

      expect(result).toEqual(
        expect.objectContaining({
          accessToken: 'new-access-token',
          refreshToken: 'new-refresh-token',
        }),
      );
    });

    it('should reject an invalid refresh token', async () => {
      mockJwtService.verifyAsync.mockRejectedValue(new Error('Invalid token'));

      await expect(service.refreshTokens('invalid-token')).rejects.toThrow(UnauthorizedException);
    });
  });
});
