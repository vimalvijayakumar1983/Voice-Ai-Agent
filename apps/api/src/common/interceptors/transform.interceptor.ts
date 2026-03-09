import {
  Injectable,
  NestInterceptor,
  ExecutionContext,
  CallHandler,
} from '@nestjs/common';
import { Observable, map } from 'rxjs';

export interface ApiResponse<T> {
  success: boolean;
  data: T;
  meta?: Record<string, unknown>;
}

@Injectable()
export class TransformInterceptor<T> implements NestInterceptor<T, ApiResponse<T>> {
  intercept(
    context: ExecutionContext,
    next: CallHandler<T>,
  ): Observable<ApiResponse<T>> {
    return next.handle().pipe(
      map((data) => {
        // If the data already has our wrapper shape, pass through
        if (
          data &&
          typeof data === 'object' &&
          'success' in (data as Record<string, unknown>)
        ) {
          return data as unknown as ApiResponse<T>;
        }

        // Extract meta if present (e.g., pagination)
        let meta: Record<string, unknown> | undefined;
        let responseData = data;

        if (
          data &&
          typeof data === 'object' &&
          'meta' in (data as Record<string, unknown>) &&
          'data' in (data as Record<string, unknown>)
        ) {
          const wrapped = data as unknown as { data: T; meta: Record<string, unknown> };
          responseData = wrapped.data;
          meta = wrapped.meta;
        }

        return {
          success: true,
          data: responseData,
          ...(meta ? { meta } : {}),
        };
      }),
    );
  }
}
