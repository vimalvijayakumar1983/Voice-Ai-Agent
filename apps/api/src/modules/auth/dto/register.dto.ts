import {
  IsEmail,
  IsString,
  MinLength,
  MaxLength,
  IsOptional,
  IsEnum,
} from 'class-validator';
import { ApiProperty, ApiPropertyOptional } from '@nestjs/swagger';

export enum Industry {
  HEALTHCARE = 'HEALTHCARE',
  FINANCE = 'FINANCE',
  INSURANCE = 'INSURANCE',
  REAL_ESTATE = 'REAL_ESTATE',
  RETAIL = 'RETAIL',
  TECHNOLOGY = 'TECHNOLOGY',
  EDUCATION = 'EDUCATION',
  HOSPITALITY = 'HOSPITALITY',
  OTHER = 'OTHER',
}

export class RegisterDto {
  @ApiProperty({ example: 'Acme Corporation' })
  @IsString()
  @MinLength(2)
  @MaxLength(100)
  companyName: string;

  @ApiProperty({ example: 'admin@acme.com' })
  @IsEmail()
  email: string;

  @ApiProperty({ minLength: 8 })
  @IsString()
  @MinLength(8)
  @MaxLength(128)
  password: string;

  @ApiProperty({ example: 'John' })
  @IsString()
  @MinLength(1)
  @MaxLength(50)
  firstName: string;

  @ApiProperty({ example: 'Doe' })
  @IsString()
  @MinLength(1)
  @MaxLength(50)
  lastName: string;

  @ApiPropertyOptional({ enum: Industry })
  @IsOptional()
  @IsEnum(Industry)
  industry?: Industry;

  @ApiPropertyOptional({ example: '+1234567890' })
  @IsOptional()
  @IsString()
  phone?: string;
}
