import { Module } from '@nestjs/common';
import { AbTestingService } from './ab-testing.service';
import { AbTestingController } from './ab-testing.controller';

@Module({
  controllers: [AbTestingController],
  providers: [AbTestingService],
  exports: [AbTestingService],
})
export class AbTestingModule {}
