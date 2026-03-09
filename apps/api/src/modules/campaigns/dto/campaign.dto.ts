import {
  IsString,
  MinLength,
  MaxLength,
  IsOptional,
  IsEnum,
  IsArray,
  IsObject,
  IsDateString,
  IsNumber,
  Min,
  Max,
} from 'class-validator';
import { ApiProperty, ApiPropertyOptional, PartialType } from '@nestjs/swagger';

export enum CampaignStatus {
  DRAFT = 'DRAFT',
  SCHEDULED = 'SCHEDULED',
  RUNNING = 'RUNNING',
  PAUSED = 'PAUSED',
  COMPLETED = 'COMPLETED',
  CANCELLED = 'CANCELLED',
}

export enum CampaignType {
  OUTBOUND = 'OUTBOUND',
  INBOUND = 'INBOUND',
  SURVEY = 'SURVEY',
  REMINDER = 'REMINDER',
}

export class CreateCampaignDto {
  @ApiProperty({ example: 'Q1 Sales Outreach' })
  @IsString()
  @MinLength(2)
  @MaxLength(100)
  name: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  @MaxLength(500)
  description?: string;

  @ApiProperty({ enum: CampaignType })
  @IsEnum(CampaignType)
  type: CampaignType;

  @ApiProperty({ description: 'Agent ID to use for calls' })
  @IsString()
  agentId: string;

  @ApiPropertyOptional({ description: 'Contact list IDs to include' })
  @IsOptional()
  @IsArray()
  @IsString({ each: true })
  contactListIds?: string[];

  @ApiPropertyOptional()
  @IsOptional()
  @IsDateString()
  scheduledStartAt?: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsDateString()
  scheduledEndAt?: string;

  @ApiPropertyOptional({ description: 'Max concurrent calls', minimum: 1, maximum: 100 })
  @IsOptional()
  @IsNumber()
  @Min(1)
  @Max(100)
  maxConcurrentCalls?: number;

  @ApiPropertyOptional({ description: 'Max calls per hour', minimum: 1 })
  @IsOptional()
  @IsNumber()
  @Min(1)
  callsPerHour?: number;

  @ApiPropertyOptional({ description: 'Calling hours configuration' })
  @IsOptional()
  @IsObject()
  callingHours?: {
    startHour: number;
    endHour: number;
    timezone: string;
    daysOfWeek: number[];
  };

  @ApiPropertyOptional()
  @IsOptional()
  @IsNumber()
  maxRetries?: number;

  @ApiPropertyOptional()
  @IsOptional()
  @IsNumber()
  retryDelayMinutes?: number;

  @ApiPropertyOptional()
  @IsOptional()
  @IsObject()
  metadata?: Record<string, unknown>;
}

export class UpdateCampaignDto extends PartialType(CreateCampaignDto) {}

export class CampaignFilterDto {
  @ApiPropertyOptional({ enum: CampaignStatus })
  @IsOptional()
  @IsEnum(CampaignStatus)
  status?: CampaignStatus;

  @ApiPropertyOptional({ enum: CampaignType })
  @IsOptional()
  @IsEnum(CampaignType)
  type?: CampaignType;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  search?: string;
}
