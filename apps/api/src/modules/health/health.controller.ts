import { Controller, Get, VERSION_NEUTRAL } from '@nestjs/common';
import { ApiTags, ApiOperation } from '@nestjs/swagger';

@ApiTags('health')
@Controller({ path: 'api/v1', version: VERSION_NEUTRAL })
export class HealthController {
  @Get('health')
  @ApiOperation({ summary: 'Public health check endpoint' })
  getHealth() {
    return {
      status: 'ok',
      timestamp: new Date().toISOString(),
    };
  }
}
