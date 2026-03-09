'use client';

import { useParams, useRouter } from 'next/navigation';
import { ArrowLeft } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ExperimentResults } from '@/components/ab-testing/experiment-results';

export default function ExperimentDetailPage() {
  const params = useParams();
  const router = useRouter();
  const experimentId = params.id as string;

  return (
    <div className="space-y-6">
      {/* Back button */}
      <div>
        <Button
          variant="ghost"
          size="sm"
          className="mb-2"
          onClick={() => router.push('/ab-testing')}
        >
          <ArrowLeft className="w-3.5 h-3.5 mr-1.5" />
          Back to Experiments
        </Button>
      </div>

      {/* Results */}
      <ExperimentResults experimentId={experimentId} />
    </div>
  );
}
