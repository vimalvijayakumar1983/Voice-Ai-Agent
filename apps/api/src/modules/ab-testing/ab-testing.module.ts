import { Module } from '@nestjs/common';
import { AbTestingService } from './ab-testing.service';
import { AbTestingController } from './ab-testing.controller';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';

@Module({
  controllers: [AbTestingController],
  providers: [AbTestingService, PrismaService, RedisService],
  exports: [AbTestingService],
})
export class AbTestingModule {}
