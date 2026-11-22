// Native process primitive only. Go owns discovery, key validation and all message reads.
import Foundation
import Darwin

do {
    guard let request = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [String: String],
          let executable = request["executable"], let container = request["container"],
          executable.hasSuffix("/Runtime/企业微信.app/Contents/MacOS/企业微信"),
          FileManager.default.isExecutableFile(atPath: executable),
          chdir(container) == 0 else { throw NSError(domain: "runtime", code: 1) }
    var attributes: posix_spawnattr_t?
    var actions: posix_spawn_file_actions_t?
    guard posix_spawnattr_init(&attributes) == 0,
          posix_spawn_file_actions_init(&actions) == 0 else { throw NSError(domain: "runtime", code: 2) }
    defer {
        posix_spawnattr_destroy(&attributes)
        posix_spawn_file_actions_destroy(&actions)
    }
    guard posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_START_SUSPENDED)) == 0 else {
        throw NSError(domain: "runtime", code: 3)
    }
    for fd in 0...2 {
        guard posix_spawn_file_actions_addopen(&actions, Int32(fd), "/dev/null", fd == 0 ? O_RDONLY : O_WRONLY, 0) == 0 else {
            throw NSError(domain: "runtime", code: 4)
        }
    }
    var environment = ProcessInfo.processInfo.environment
    environment["CFFIXED_USER_HOME"] = container
    var args = [strdup(executable), nil]
    var env = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { args.forEach { free($0) }; env.forEach { free($0) } }
    var pid: pid_t = 0
    let code = posix_spawn(&pid, executable, &actions, &attributes, &args, &env)
    guard code == 0 else { throw NSError(domain: "runtime", code: Int(code)) }
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: ["pid": pid]))
} catch {
    FileHandle.standardOutput.write(Data("{\"error\":\"临时副本启动未完成\"}".utf8))
    exit(1)
}
