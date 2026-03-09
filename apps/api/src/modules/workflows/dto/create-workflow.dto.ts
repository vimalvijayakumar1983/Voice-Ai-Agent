import {
  IsString,
  MinLength,
  MaxLength,
  IsOptional,
  IsArray,
  IsObject,
  ValidateNested,
  IsBoolean,
} from 'class-validator';
import { Type } from 'class-transformer';
import { ApiProperty, ApiPropertyOptional, PartialType } from '@nestjs/swagger';

export class WorkflowNode {
  @ApiProperty()
  @IsString()
  id: string;

  @ApiProperty()
  @IsString()
  type: string;

  @ApiProperty()
  @IsObject()
  position: { x: number; y: number };

  @ApiProperty()
  @IsObject()
  data: Record<string, unknown>;
}

export class WorkflowEdge {
  @ApiProperty()
  @IsString()
  id: string;

  @ApiProperty()
  @IsString()
  source: string;

  @ApiProperty()
  @IsString()
  target: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  label?: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsObject()
  conditions?: Record<string, unknown>;
}

export class CreateWorkflowDto {
  @ApiProperty({ example: 'Lead Qualification Flow' })
  @IsString()
  @MinLength(2)
  @MaxLength(100)
  name: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  @MaxLength(500)
  description?: string;

  @ApiProperty({ type: [WorkflowNode] })
  @IsArray()
  @ValidateNested({ each: true })
  @Type(() => WorkflowNode)
  nodes: WorkflowNode[];

  @ApiProperty({ type: [WorkflowEdge] })
  @IsArray()
  @ValidateNested({ each: true })
  @Type(() => WorkflowEdge)
  edges: WorkflowEdge[];

  @ApiPropertyOptional()
  @IsOptional()
  @IsString()
  agentId?: string;

  @ApiPropertyOptional()
  @IsOptional()
  @IsBoolean()
  isActive?: boolean;

  @ApiPropertyOptional()
  @IsOptional()
  @IsObject()
  metadata?: Record<string, unknown>;
}

export class UpdateWorkflowDto extends PartialType(CreateWorkflowDto) {}
