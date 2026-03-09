import {
  Injectable,
  NestInterceptor,
  ExecutionContext,
  CallHandler,
} from '@nestjs/common';
import { Observable, tap } from 'rxjs';
import { PrismaService } from '../services/prisma.service';

@Injectable()
export class AuditLogInterceptor implements NestInterceptor {
  constructor(private readonly prisma: PrismaService) {}

  intercept(context: ExecutionContext, next: CallHandler): Observable<unknown> {
    const request = context.switchToHttp().getRequest();
    const method = request.method;

    // Only log mutations
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      return next.handle();
    }

    const startTime = Date.now();
    const user = request.user;
    const action = `${method} ${request.route?.path || request.url}`;

    return next.handle().pipe(
      tap({
        next: (responseBody) => {
          this.logAudit({
            userId: user?.sub || null,
            tenantId: user?.tenantId || null,
            action,
            resource: context.getClass().name,
            resourceId: request.params?.id || null,
            details: {
              method,
              path: request.url,
              body: this.sanitizeBody(request.body),
              statusCode: context.switchToHttp().getResponse().statusCode,
              duration: Date.now() - startTime,
            },
            ipAddress: request.ip,
            userAgent: request.headers['user-agent'] || null,
          }).catch(() => {
            // Silently fail - audit logging should not break requests
          });
        },
        error: (error) => {
          this.logAudit({
            userId: user?.sub || null,
            tenantId: user?.tenantId || null,
            action,
            resource: context.getClass().name,
            resourceId: request.params?.id || null,
            details: {
              method,
              path: request.url,
              body: this.sanitizeBody(request.body),
              error: error.message,
              statusCode: error.status || 500,
              duration: Date.now() - startTime,
            },
            ipAddress: request.ip,
            userAgent: request.headers['user-agent'] || null,
          }).catch(() => {
            // Silently fail
          });
        },
      }),
    );
  }

  private async logAudit(data: {
    userId: string | null;
    tenantId: string | null;
    action: string;
    resource: string;
    resourceId: string | null;
    details: Record<string, unknown>;
    ipAddress: string;
    userAgent: string | null;
  }): Promise<void> {
    await this.prisma.auditLog.create({ data });
  }

  private sanitizeBody(body: Record<string, unknown>): Record<string, unknown> {
    if (!body) return {};
    const sanitized = { ...body };
    const sensitiveFields = ['password', 'token', 'secret', 'apiKey', 'creditCard'];
    for (const field of sensitiveFields) {
      if (sanitized[field]) {
        sanitized[field] = '[REDACTED]';
      }
    }
    return sanitized;
  }
}
