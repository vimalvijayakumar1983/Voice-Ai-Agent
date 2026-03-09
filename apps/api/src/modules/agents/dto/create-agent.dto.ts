import {
  IsString,
  MinLength,
  MaxLength,
  IsOptional,
  IsEnum,
  IsArray,
  IsObject,
  IsBoolean,
  IsNumber,
  ValidateNested,
} from 'class-validator';
import { Type } from 'class-transformer';
import { ApiProperty, ApiPropertyOptional } from '@nestjs/swagger';

export enum AgentType {
  INBOUND = 'INBOUND',
  OUTBOUND = 'OUTBOUND',
  HYBRID = 'HYBRID',
}

export enum AgentVoice {
  MALE_1 = 'MALE_1',
  MALE_2 = 'MALE_2',
  FEMALE_1 = 'FEMALE_1',
  FEMALE_2 = 'FEMALE_2',
  NEUTRAL = 'NEUTRAL',
}

export enum Personality {
  PROFESSIONAL = 'PROFESSIONAL',
  FRIENDLY = 'FRIENDLY',
  EMPATHETIC = 'EMPATHETIC',
  ASSERTIVE = 'ASSERTIVE',
  CASUAL = 'CASUAL',
}

export enum Tone {
  FORMAL = 'FORMAL',
  CONVERSATIONAL = 'CONVERSATIONAL',
  ENTHUSIASTIC = 'ENTHUSIASTIC',
  CALM = 'CALM',
  NEUTRAL = 'NEUTRAL',
}

export class EscalationRule {
  @ApiProperty()
  @IsString()
  condition: string;

  @ApiProperty()
  @IsString()
  action: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  targetNumber?: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  targetAgent?: string;
}

export class ComplianceBlock {
  @ApiProperty()
  @IsString()
  name: string;

  @ApiProperty()
  @IsString()
  script: string;

  @ApiProperty()
  @IsBoolean()
  required: boolean;
}

export class CreateAgentDto {
  @ApiProperty({ example: 'Sales Assistant' })
  @IsString()
  @MinLength(2)
  @MaxLength(100)
  name: string;

  @ApiPropertyOptional({ maxLength: 500 })
  @IsOptional()
  @IsString()
  @MaxLength(500)
  description?: string;

  @ApiProperty({ enum: AgentType })
  @IsEnum(AgentType)
  type: AgentType;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  industry?: string;

  @ApiProperty({ enum: AgentVoice })
  @IsEnum(AgentVoice)
  voice: AgentVoice;

  @ApiProperty({ example: 'en-US' })
  @IsString()
  language: string;

  @ApiPropertyOptional({ enum: Personality })
  @IsOptional()
  @IsEnum(Personality)
  personality?: Personality;

  @ApiPropertyOptional({ enum: Tone })
  @IsOptional()
  @IsEnum(Tone)
  tone?: Tone;

  @ApiProperty({ description: 'System prompt defining agent behavior' })
  @IsString()
  @MinLength(10)
  @MaxLength(10000)
  systemPrompt: string;

  @ApiPropertyOptional({ description: 'Greeting message for calls' })
  @IsOptional()
  @IsString()
  @MaxLength(1000)
  greeting?: string;

  @ApiPropertyOptional({ type: [EscalationRule] })
  @IsOptional()
  @IsArray()
  @ValidateNested({ each: true })
  @Type(() => EscalationRule)
  escalationRules?: EscalationRule[];

  @ApiPropertyOptional({ type: [ComplianceBlock] })
  @IsOptional()
  @IsArray()
  @ValidateNested({ each: true })
  @Type(() => ComplianceBlock)
  complianceBlocks?: ComplianceBlock[];

  @ApiPropertyOptional({ description: 'Primary objective of calls' })
  @IsOptional()
  @IsString()
  @MaxLength(500)
  callObjective?: string;

  @ApiPropertyOptional({ description: 'Criteria to measure success' })
  @IsOptional()
  @IsArray()
  @IsString({ each: true })
  successCriteria?: string[];

  @ApiPropertyOptional()
  @IsOptional()
  @IsNumber()
  maxCallDurationSeconds?: number;

  @ApiPropertyOptional()
  @IsOptional()
  @IsNumber()
  silenceTimeoutSeconds?: number;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  llmProvider?: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  llmModel?: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsNumber()
  temperature?: number;

  @ApiPropertyOptional({ description: 'Associated knowledge base IDs' })
  @IsOptional()
  @IsArray()
  @IsString({ each: true })
  knowledgeBaseIds?: string[];

  @ApiPropertyOptional({ description: 'Custom functions/tools the agent can call' })
  @IsOptional()
  @IsArray()
  @IsObject({ each: true })
  tools?: Record<string, unknown>[];

  @ApiPropertyOptional()
  @IsOptional()
  @IsObject()
  metadata?: Record<string, unknown>;
}
