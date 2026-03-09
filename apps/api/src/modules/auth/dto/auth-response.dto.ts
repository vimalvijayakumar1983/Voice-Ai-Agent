import { ApiProperty } from '@nestjs/swagger';

export class AuthTokensDto {
  @ApiProperty()
  accessToken: string;

  @ApiProperty()
  refreshToken: string;

  @ApiProperty()
  expiresIn: number;

  @ApiProperty()
  tokenType: string;
}

export class AuthUserDto {
  @ApiProperty()
  id: string;

  @ApiProperty()
  email: string;

  @ApiProperty()
  firstName: string;

  @ApiProperty()
  lastName: string;

  @ApiProperty()
  role: string;

  @ApiProperty()
  tenantId: string;

  @ApiProperty()
  tenantName: string;
}

export class AuthResponseDto {
  @ApiProperty()
  user: AuthUserDto;

  @ApiProperty()
  tokens: AuthTokensDto;
}

export class RefreshTokenDto {
  @ApiProperty()
  refreshToken: string;
}

export class ForgotPasswordDto {
  @ApiProperty()
  email: string;
}
