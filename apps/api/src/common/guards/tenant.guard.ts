import { Injectable, CanActivate, ExecutionContext, ForbiddenException } from '@nestjs/common';

@Injectable()
export class TenantGuard implements CanActivate {
  canActivate(context: ExecutionContext): boolean {
    const request = context.switchToHttp().getRequest();
    const user = request.user;
    const tenantIdParam = request.params.tenantId || request.headers['x-tenant-id'];

    if (!user) {
      throw new ForbiddenException('Authentication required');
    }

    // Super admins can access any tenant
    if (user.role === 'SUPER_ADMIN') {
      return true;
    }

    // If a tenantId is in the route or header, verify the user belongs to it
    if (tenantIdParam && user.tenantId !== tenantIdParam) {
      throw new ForbiddenException('You do not have access to this tenant');
    }

    if (!user.tenantId) {
      throw new ForbiddenException('No tenant associated with current user');
    }

    return true;
  }
}
