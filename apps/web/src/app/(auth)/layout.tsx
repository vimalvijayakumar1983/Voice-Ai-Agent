import { Zap } from 'lucide-react';

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen">
      {/* Left branding panel */}
      <div className="hidden w-1/2 bg-gradient-to-br from-primary via-primary/90 to-blue-700 p-12 lg:flex lg:flex-col lg:justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-white/20 backdrop-blur-sm">
            <Zap className="h-6 w-6 text-white" />
          </div>
          <span className="text-xl font-bold text-white">VoiceAI</span>
        </div>
        <div className="space-y-6">
          <h1 className="text-4xl font-bold leading-tight text-white">
            Enterprise Voice AI
            <br />
            Agent Platform
          </h1>
          <p className="max-w-md text-lg text-white/80">
            Deploy intelligent voice agents at scale. Automate outbound calls,
            handle inbound inquiries, and drive conversions with AI-powered
            conversations.
          </p>
          <div className="grid grid-cols-3 gap-6 pt-8">
            <div>
              <p className="text-3xl font-bold text-white">10M+</p>
              <p className="text-sm text-white/60">Calls Processed</p>
            </div>
            <div>
              <p className="text-3xl font-bold text-white">98.5%</p>
              <p className="text-sm text-white/60">Uptime</p>
            </div>
            <div>
              <p className="text-3xl font-bold text-white">500+</p>
              <p className="text-sm text-white/60">Enterprise Clients</p>
            </div>
          </div>
        </div>
        <p className="text-xs text-white/40">
          SOC 2 Type II Certified &middot; HIPAA Compliant &middot; GDPR Ready
        </p>
      </div>

      {/* Right form panel */}
      <div className="flex w-full items-center justify-center p-8 lg:w-1/2">
        <div className="w-full max-w-md">{children}</div>
      </div>
    </div>
  );
}
