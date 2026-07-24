// A stand-in "application" for the memscout remote-collection worked example.
//
// It heap-allocates a few Session objects with known fields, prints each one (so
// you can see the ground truth), then parks in pause() so a collection script can
// inspect it live. Think of it as "the app the reporter is running."
//
// Layout is fixed and documented so the developer can hand offsets to the reporter
// script (x86-64, Itanium C++ ABI):
//
//   offset  field        type
//        0  <vptr>       (pointer to Session's vtable + 16)
//        8  mActive      bool
//       12  mId          int32_t
//       16  mRequests    uint64_t
//       24  mUser        const char*   (ns[C]String-style: data pointer ...)
//       32  mUserLen     uint32_t      (... then length -> "24:nscstring:mUser")
//
// Build:  c++ demo_target.cpp -O0 -o demo_target   (symbols kept: _ZTV7Session)

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <unistd.h>

class Session {
 public:
  virtual ~Session();
  virtual int kind();
  bool mActive;
  int32_t mId;
  uint64_t mRequests;
  const char* mUser;
  uint32_t mUserLen;
};

Session::~Session() {}
int Session::kind() { return 1; }

int main() {
  static const char* kUsers[] = {"alice", "bob", "carol"};
  const int kCount = 3;

  for (int i = 0; i < kCount; ++i) {
    Session* s = new Session();
    s->mActive = (i != 1);
    s->mId = 1000 + i;
    s->mRequests = 100 + i * 7;
    s->mUser = kUsers[i];
    s->mUserLen = (uint32_t)strlen(kUsers[i]);
    printf("session %p id=%d active=%d requests=%llu user=%s\n",
           (void*)s, s->mId, s->mActive ? 1 : 0,
           (unsigned long long)s->mRequests, s->mUser);
  }

  printf("READY pid=%d\n", (int)getpid());
  fflush(stdout);

  pause();  // stay alive so the collection script can inspect us
  return 0;
}
