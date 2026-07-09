import { Switch, Route, Router } from 'wouter';
import { useHashLocation } from 'wouter/use-hash-location';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from '@/lib/queryClient';
import SlatePage from '@/pages/SlatePage';
import SlipPage from '@/pages/SlipPage';
import ResultsPage from '@/pages/ResultsPage';
import BottomNav from '@/components/BottomNav';

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router hook={useHashLocation}>
        <div id="app-shell" style={{ display: 'flex', flexDirection: 'column', minHeight: '100dvh' }}>
          <main className="app-content">
            <Switch>
              <Route path="/" component={SlatePage} />
              <Route path="/slip" component={SlipPage} />
              <Route path="/results" component={ResultsPage} />
              <Route component={SlatePage} />
            </Switch>
          </main>
          <BottomNav />
        </div>
      </Router>
    </QueryClientProvider>
  );
}
