import { SetMetadata } from '@nestjs/common';

export enum Role {
  SUPER_ADMIN = 'SUPER_ADMIN',
  TENANT_OWNER = 'TENANT_OWNER',
  TENANT_ADMIN = 'TENANT_ADMIN',
  MANAGER = 'MANAGER',
  AGENT = 'AGENT',
  VIEWER = 'VIEWER',
}

export const ROLES_KEY = 'roles';
export const Roles = (...roles: Role[]) => SetMetadata(ROLES_KEY, roles);
