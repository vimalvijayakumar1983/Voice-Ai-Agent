import { ValidationPipe, ValidationPipeOptions } from '@nestjs/common';

export const validationPipeConfig: ValidationPipeOptions = {
  whitelist: true,
  forbidNonWhitelisted: true,
  transform: true,
  transformOptions: {
    enableImplicitConversion: true,
  },
  stopAtFirstError: false,
  disableErrorMessages: false,
};

export class AppValidationPipe extends ValidationPipe {
  constructor() {
    super(validationPipeConfig);
  }
}
