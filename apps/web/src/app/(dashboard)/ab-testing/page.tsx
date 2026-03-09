'use client';

import { useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { FlaskConical, Plus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ExperimentList } from '@/components/ab-testing/experiment-list';
import { ExperimentForm } from '@/components/ab-testing/experiment-form';

export default function AbTestingPage() {
  const router = useRouter();
  const [showCreateDialog, setShowCreateDialog] = useState(false);

  const handleViewExperiment = useCallback(
    (id: string) => {
      router.push(`/ab-testing/${id}`);
    },
    [router],
  );

  const handleCreateSuccess = useCallback(
    (id: string) => {
      setShowCreateDialog(false);
      router.push(`/ab-testing/${id}`);
    },
    [router],
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <FlaskConical className="w-6 h-6" />
            A/B Testing
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Test different prompt variants to optimize agent performance
          </p>
        </div>
        <Button onClick={() => setShowCreateDialog(true)}>
          <Plus className="w-4 h-4 mr-1.5" />
          New Experiment
        </Button>
      </div>

      {/* Experiment List */}
      <Card>
        <CardContent className="p-0">
          <ExperimentList onViewExperiment={handleViewExperiment} />
        </CardContent>
      </Card>

      {/* Create Dialog */}
      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Create New Experiment</DialogTitle>
          </DialogHeader>
          <ExperimentForm
            onSuccess={handleCreateSuccess}
            onCancel={() => setShowCreateDialog(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}
