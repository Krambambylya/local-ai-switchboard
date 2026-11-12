import { ChatProvider } from "../features/chat/ChatProvider";
import { AppShell } from "../components/layout/AppShell";

export function App() {
  return (
    <ChatProvider>
      <AppShell />
    </ChatProvider>
  );
}
